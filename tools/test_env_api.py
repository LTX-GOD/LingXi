"""
测试环境 API 工具
=================
用于非比赛平台场景（如独立 Web 靶机 URL）的渗透交互。

特点：
- 不依赖 COMPETITION_BASE_URL
- 直接对目标 URL 发起 HTTP 请求
- 返回结构化结果，便于 LLM 继续利用
"""

import json
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
from langchain_core.tools import tool

from tools.flag_utils import extract_flags


def _normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    # 未带协议默认按 http 处理；443 端口时自动切到 https
    if re.search(r":443(?:/|$)", raw):
        return f"https://{raw}"
    return f"http://{raw}"


def _normalize_and_encode_query(url: str) -> str:
    """
    规范化 URL，并对查询参数值做 percent-encoding。

    例如:
      ?url=system('ls /');
    会转成:
      ?url=system%28%27ls%20%2F%27%29%3B
    """
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not query_pairs:
            return url
        encoded_query = urlencode(query_pairs, doseq=True, quote_via=quote)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded_query, parts.fragment))
    except Exception:
        return url


def _safe_preview(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated {len(text) - limit} chars] ..."


@tool
def testenv_http_request(
    url: str,
    method: str = "GET",
    body: str = "",
    headers_json: str = "{}",
    timeout: int = 20,
    verify_tls: bool = False,
    normalize_query: bool = True,
) -> str:
    """
    对测试环境目标发送 HTTP 请求（独立于比赛平台 API）。

    Args:
        url: 目标 URL（可省略协议，如 example.com:443/path）
        method: HTTP 方法，默认 GET
        body: 请求体字符串（POST/PUT/PATCH 可用）
        headers_json: 请求头 JSON 字符串，如 {"Content-Type":"application/json"}
        timeout: 超时时间（秒）
        verify_tls: 是否校验证书（测试环境常为自签名，默认 false）
        normalize_query: 是否自动编码 query 参数，默认 true
    """
    target = _normalize_url(url)
    if not target:
        return "错误：url 不能为空"
    if normalize_query:
        target = _normalize_and_encode_query(target)

    try:
        parsed_headers = json.loads(headers_json) if headers_json else {}
        if not isinstance(parsed_headers, dict):
            return "错误：headers_json 必须是 JSON 对象"
    except Exception as e:
        return f"错误：headers_json 解析失败: {e}"

    req_method = (method or "GET").upper().strip()
    if req_method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return f"错误：不支持的 method: {req_method}"

    try:
        resp = requests.request(
            method=req_method,
            url=target,
            headers=parsed_headers,
            data=(body if body else None),
            timeout=max(1, int(timeout)),
            verify=bool(verify_tls),
            allow_redirects=True,
        )
    except Exception as e:
        return f"请求失败: {e}"

    text = resp.text or ""
    flags = extract_flags(text)

    result = {
        "request": {
            "method": req_method,
            "url": target,
            "verify_tls": bool(verify_tls),
            "normalize_query": bool(normalize_query),
        },
        "response": {
            "status_code": resp.status_code,
            "final_url": resp.url,
            "headers": dict(resp.headers),
            "body_preview": _safe_preview(text),
        },
        "candidates": {
            "flags": flags,
        },
    }

    # 额外提取页面中可能的 base64 常量，便于快速发现前端硬编码 flag
    b64_hits = re.findall(r"[A-Za-z0-9+/]{16,}={0,2}", text)
    if b64_hits:
        result["candidates"]["base64_like"] = b64_hits[:20]

    return json.dumps(result, ensure_ascii=False, indent=2)
