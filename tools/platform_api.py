"""
比赛平台 API 工具
================
封装腾讯 Hackathon 比赛平台的所有 API 接口 (v2 — 对齐官方文档):
  GET  /api/challenges       — 获取赛题列表
  POST /api/start_challenge   — 启动赛题实例
  POST /api/stop_challenge    — 停止赛题实例
  POST /api/submit            — 提交 Flag

认证方式: Agent-Token Header
频率限制: 每队每秒最多 3 次
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from langchain_core.tools import tool
from requests.exceptions import RequestException, Timeout

from host_failover import (
    HostFailoverState,
    is_failover_worthy_http_response,
    normalize_host_url,
)
from log_utils import extract_target_hosts, flag_fingerprint, safe_endpoint_label
from tools.api_gateway import get_api_gateway, RequestPriority

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    ClientSession = None
    streamablehttp_client = None

logger = logging.getLogger(__name__)
_PLATFORM_API_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(8, int(os.getenv("PLATFORM_API_EXECUTOR_WORKERS", "16"))),
    thread_name_prefix="platform-api",
)
_SUBMITTED_FLAGS_GLOBAL: set[tuple[str, str]] = set()
_RESCUED_FLAGS_GLOBAL: set[tuple[str, str]] = set()


# ─── 异常定义 ───


class APIError(Exception):
    """API 异常基类"""

    pass


class RateLimitError(APIError):
    """频率限制 (429)"""

    pass


class APIHostFailoverError(APIError):
    """可触发主赛场 host 回退的 API 异常。"""


class CompetitionMCPError(Exception):
    """主赛场 MCP 异常基类。"""


class CompetitionMCPTransportError(CompetitionMCPError):
    """主赛场 MCP 传输层异常，可触发 host 回退。"""


class CompetitionMCPAuthError(CompetitionMCPError):
    """主赛场 MCP 认证异常，不触发 host 回退。"""


class FlagRescueNotice(Exception):
    """Flag 已缓存，但当前尚未实际得分。"""


def _flag_submit_key(code: str, flag: str) -> tuple[str, str]:
    return (str(code or "").strip(), str(flag or "").strip())


def _resolve_flag_rescue_path() -> Path:
    raw = str(os.getenv("LINGXI_FLAG_RESCUE_PATH", "") or "").strip()
    return Path(raw) if raw else Path("/tmp/SAVED_FLAGS_RESCUE.txt")


def _resolve_flag_recovery_delays() -> tuple[int, ...]:
    raw = str(os.getenv("LINGXI_FLAG_RECOVERY_DELAYS", "2,4") or "").strip()
    delays: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            delays.append(max(0, int(item)))
        except ValueError:
            continue
    return tuple(delays or (2, 4))


def _append_flag_rescue_record(code: str, flag: str, reason: str) -> str:
    key = _flag_submit_key(code, flag)
    path = _resolve_flag_rescue_path()
    if key in _RESCUED_FLAGS_GLOBAL:
        return str(path)

    _RESCUED_FLAGS_GLOBAL.add(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"code={code} flag={flag} reason={reason} ts={int(time.time())}\n"
            )
    except Exception as exc:
        logger.warning(
            "[API] 缓存待补提 Flag 失败: code=%s flag=%s err=%s",
            code,
            flag_fingerprint(flag),
            exc,
        )
        return "<write_failed>"
    return str(path)


def _submit_answer_with_instance_recovery(
    client: "CompetitionAPIClient",
    code: str,
    flag: str,
) -> tuple[Dict[str, Any], str]:
    try:
        return client.submit_answer(code, flag), ""
    except APIError as exc:
        error_text = str(exc)
        if "赛题实例未运行" not in error_text:
            raise

    rescue_path = _append_flag_rescue_record(code, flag, reason=error_text)
    recovery_errors = [error_text]
    logger.warning(
        "[API] 提交时实例未运行，开始自动补救: code=%s flag=%s cache=%s",
        code,
        flag_fingerprint(flag),
        rescue_path,
    )
    try:
        client.start_challenge(code)
    except Exception as start_exc:
        detail = str(start_exc)
        recovery_errors.append(detail)
        raise FlagRescueNotice(
            f"⚠️ 赛题实例未运行，FLAG 已缓存到 {rescue_path}，本次尚未得分。"
            f"自动拉起实例失败：{detail}"
        ) from start_exc

    for delay in _resolve_flag_recovery_delays():
        if delay > 0:
            time.sleep(delay)
        try:
            data = client.submit_answer(code, flag)
            return (
                data,
                f"⚠️ 提交时实例曾离线，已自动拉起实例并补提成功。缓存位置: {rescue_path}",
            )
        except APIError as retry_exc:
            retry_text = str(retry_exc)
            recovery_errors.append(retry_text)
            if "赛题实例未运行" not in retry_text:
                raise

    raise FlagRescueNotice(
        f"⚠️ 赛题实例未运行，FLAG 已缓存到 {rescue_path}，本次尚未得分。"
        f"已尝试自动恢复但仍失败：{' | '.join(recovery_errors[-3:])}"
    )


# ─── 重试装饰器 ───


def retry_on_error(max_retries: int = 5, base_delay: float = 2.0):
    """自动重试装饰器 — 处理频率限制 + 网络错误 + 502/503"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except RateLimitError as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"[API] 频率限制，{delay:.0f}s 后重试 ({attempt + 1}/{max_retries})"
                        )
                        time.sleep(delay)
                except (requests.ConnectionError, Timeout) as e:
                    last_exc = APIError(f"网络错误: {e}")
                    if attempt < max_retries:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"[API] 网络错误，{delay:.0f}s 后重试 ({attempt + 1}/{max_retries})"
                        )
                        time.sleep(delay)
                except APIError as e:
                    if any(code in str(e) for code in ["502", "503", "504"]):
                        last_exc = e
                        if attempt < max_retries:
                            delay = base_delay * (2**attempt)
                            time.sleep(delay)
                            continue
                    raise
            raise last_exc

        return wrapper

    return decorator


# ─── API 客户端 ───


class CompetitionAPIClient:
    """
    比赛平台 API 客户端 — 对齐官方文档 v2

    认证: Agent-Token header
    频率: 通过统一API网关管理 (全局 3 req/s)
    """

    def __init__(
        self,
        base_url: str = "",
        api_token: str = "",
        fallback_base_url: str = "",
    ):
        resolved_base_url = normalize_host_url(
            base_url
            or os.getenv("COMPETITION_API_BASE_URL", "")
            or os.getenv("COMPETITION_BASE_URL", ""),
            default_scheme="http",
        )
        resolved_fallback_base_url = normalize_host_url(
            fallback_base_url or os.getenv("COMPETITION_SERVER_HOST_FALLBACK", ""),
            default_scheme="http",
        )
        self.base_url = resolved_base_url
        self._host_failover = HostFailoverState(
            primary=resolved_base_url,
            fallback=resolved_fallback_base_url,
            threshold=2,
            default_scheme="http",
        )
        # 官方文档: AGENT_TOKEN / 兼容旧配置: COMPETITION_API_TOKEN
        self.api_token = (
            api_token
            or os.getenv("AGENT_TOKEN", "")
            or os.getenv("COMPETITION_API_TOKEN", "")
        )
        # ✅ 官方认证头: Agent-Token (不是 Authorization Bearer)
        self.headers = {
            "Agent-Token": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # 主战场使用独立网关，避免与论坛赛道互相抢限额/退避状态
        self._gateway = get_api_gateway("platform")
        logger.info(
            "[API] 初始化: primary=%s fallback=%s",
            safe_endpoint_label(self.base_url),
            safe_endpoint_label(self.fallback_base_url) or "<disabled>",
        )

    @property
    def active_base_url(self) -> str:
        return self._host_failover.snapshot().active

    @property
    def fallback_base_url(self) -> str:
        return self._host_failover.snapshot().fallback

    def describe_transport(self) -> Dict[str, Any]:
        snapshot = self._host_failover.snapshot()
        return {
            "protocol": "http-api",
            "auth_mode": "Agent-Token",
            "primary": snapshot.primary,
            "fallback": snapshot.fallback,
            "active": snapshot.active,
            "failure_streak": snapshot.failure_streak,
            "switched": snapshot.switched,
        }

    def _rate_limit(self, priority: RequestPriority = RequestPriority.NORMAL, endpoint: str = "unknown"):
        """通过统一网关获取请求许可"""
        self._gateway.acquire(priority=priority, endpoint=endpoint)

    def _ensure_base_url(self):
        if not self.base_url:
            raise APIError(
                "COMPETITION_API_BASE_URL / COMPETITION_BASE_URL 未配置。当前任务若为测试环境，请使用 testenv_http_request 工具而非比赛平台工具。"
            )
        if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            raise APIError(
                f"比赛平台 API 地址非法: {self.base_url}（需包含 http:// 或 https://）"
            )

    def _handle_response(self, resp: requests.Response, endpoint: str = "unknown") -> Dict[str, Any]:
        """
        统一响应解析 — 官方格式:
        {code: 0, message: "success", data: ...}
        """
        if resp.status_code == 429:
            # 通知网关触发429
            self._gateway.report_429(endpoint=endpoint)
            raise RateLimitError("频率限制: 每秒最多调用3次")

        # 成功请求，重置退避计数
        if resp.status_code == 200:
            self._gateway.reset_backoff()

        try:
            body = resp.json()
        except ValueError:
            error_cls = (
                APIHostFailoverError
                if is_failover_worthy_http_response(
                    resp.status_code,
                    resp.headers.get("content-type", ""),
                )
                else APIError
            )
            raise error_cls(f"HTTP {resp.status_code}: 非JSON响应 — {resp.text[:200]}")

        if resp.status_code == 200:
            # 官方格式: code=0 成功, code=-1 失败
            if body.get("code") == 0:
                return body
            else:
                raise APIError(f"平台返回错误: {body.get('message', '未知错误')}")
        else:
            error_cls = (
                APIHostFailoverError
                if resp.status_code not in {401, 403}
                and is_failover_worthy_http_response(
                    resp.status_code,
                    resp.headers.get("content-type", ""),
                )
                else APIError
            )
            raise error_cls(f"HTTP {resp.status_code}: {body.get('message', resp.text)}")

    def _record_base_url_success(self, base_url: str) -> None:
        self._host_failover.record_success(base_url)

    def _record_base_url_failure(self, base_url: str, reason: str) -> bool:
        snapshot, switched = self._host_failover.record_failure(base_url)
        if not snapshot.fallback or normalize_host_url(base_url) != snapshot.primary:
            return False
        logger.warning(
            "[API] 主域名访问失败 (%s/%s): host=%s reason=%s",
            snapshot.failure_streak,
            snapshot.threshold,
            safe_endpoint_label(snapshot.primary),
            reason,
        )
        if switched:
            logger.warning(
                "[API] 已切换到主赛场内网回退入口: primary=%s fallback=%s",
                safe_endpoint_label(snapshot.primary),
                safe_endpoint_label(snapshot.fallback),
            )
        return switched

    def _request_with_base_url(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        endpoint: str,
        priority: RequestPriority,
        timeout: int,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._rate_limit(priority=priority, endpoint=endpoint)
        url = f"{base_url}{path}"
        if method.upper() == "GET":
            response = requests.get(url, headers=self.headers, timeout=timeout)
        else:
            response = requests.post(
                url,
                headers=self.headers,
                json=json_body,
                timeout=timeout,
            )
        body = self._handle_response(response, endpoint=endpoint)
        self._record_base_url_success(base_url)
        return body

    @retry_on_error()
    def _request(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        priority: RequestPriority,
        timeout: int,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._ensure_base_url()
        base_url = self.active_base_url or self.base_url
        try:
            return self._request_with_base_url(
                base_url,
                method,
                path,
                endpoint=endpoint,
                priority=priority,
                timeout=timeout,
                json_body=json_body,
            )
        except APIHostFailoverError as exc:
            if self._record_base_url_failure(base_url, str(exc)):
                return self._request_with_base_url(
                    self.active_base_url,
                    method,
                    path,
                    endpoint=endpoint,
                    priority=priority,
                    timeout=timeout,
                    json_body=json_body,
                )
            raise
        except (requests.ConnectionError, Timeout) as exc:
            if self._record_base_url_failure(base_url, f"network-error: {exc}"):
                return self._request_with_base_url(
                    self.active_base_url,
                    method,
                    path,
                    endpoint=endpoint,
                    priority=priority,
                    timeout=timeout,
                    json_body=json_body,
                )
            raise
        except RequestException as exc:
            if self._record_base_url_failure(base_url, f"request-error: {exc}"):
                return self._request_with_base_url(
                    self.active_base_url,
                    method,
                    path,
                    endpoint=endpoint,
                    priority=priority,
                    timeout=timeout,
                    json_body=json_body,
                )
            raise APIError(f"网络错误: {exc}") from exc

    # ─── 核心 API ───

    def get_challenges(self) -> Dict[str, Any]:
        """
        获取当前关卡及之前关卡的赛题列表
        GET /api/challenges
        """
        body = self._request(
            "GET",
            "/api/challenges",
            endpoint="get_challenges",
            priority=RequestPriority.LOW,
            timeout=15,
        )
        data = body.get("data", {})
        challenges = data.get("challenges", [])
        logger.info(
            f"[API] 获取 {len(challenges)} 道题目 "
            f"(关卡: {data.get('current_level', '?')}, "
            f"已解: {data.get('solved_challenges', 0)})"
        )
        return data

    def start_challenge(self, code: str) -> Dict[str, Any]:
        """
        启动赛题实例 — 每队同时最多运行 3 个实例
        POST /api/start_challenge  body: {code}
        返回入口地址列表
        """
        payload = {"code": code}
        body = self._request(
            "POST",
            "/api/start_challenge",
            endpoint="start_challenge",
            priority=RequestPriority.HIGH,
            timeout=30,
            json_body=payload,
        )
        data = body.get("data", body.get("message", ""))
        targets = ",".join(extract_target_hosts(json.dumps(data, ensure_ascii=False))) or "n/a"
        size = len(data) if isinstance(data, list) else 0
        logger.info("[API] 启动实例: %s target=%s entries=%s", code, targets, size)
        return {"message": body.get("message", ""), "data": data}

    def stop_challenge(self, code: str) -> Dict[str, Any]:
        """
        停止赛题实例
        POST /api/stop_challenge  body: {code}
        """
        payload = {"code": code}
        body = self._request(
            "POST",
            "/api/stop_challenge",
            endpoint="stop_challenge",
            priority=RequestPriority.HIGH,
            timeout=15,
            json_body=payload,
        )
        logger.info(f"[API] 停止实例: {code}")
        return {"message": body.get("message", "")}

    def submit_answer(self, code: str, flag: str) -> Dict[str, Any]:
        """
        提交 Flag
        POST /api/submit  body: {code, flag}
        """
        payload = {"code": code, "flag": flag}
        body = self._request(
            "POST",
            "/api/submit",
            endpoint="submit_answer",
            priority=RequestPriority.CRITICAL,
            timeout=15,
            json_body=payload,
        )
        data = body.get("data", {})
        correct = data.get("correct", False)
        symbol = "✅" if correct else "❌"
        logger.info(
            "[API] %s 提交 %s: flag=%s result=%s (%s/%s)",
            symbol,
            code,
            flag_fingerprint(flag),
            "正确" if correct else "错误",
            data.get("flag_got_count", 0),
            data.get("flag_count", "?"),
        )
        return data

class CompetitionMCPClient:
    """主赛场官方 MCP 客户端（streamable-http）。"""

    def __init__(
        self,
        server_host: str = "",
        api_token: str = "",
        server_host_fallback: str = "",
    ):
        resolved_server_host = normalize_host_url(
            server_host
            or os.getenv("COMPETITION_SERVER_HOST", "")
            or os.getenv("COMPETITION_API_BASE_URL", "")
            or os.getenv("COMPETITION_BASE_URL", ""),
            default_scheme="http",
        )
        resolved_fallback_host = normalize_host_url(
            server_host_fallback or os.getenv("COMPETITION_SERVER_HOST_FALLBACK", ""),
            default_scheme="http",
        )
        self.server_host = resolved_server_host
        self.api_token = (
            api_token
            or os.getenv("AGENT_TOKEN", "")
            or os.getenv("COMPETITION_API_TOKEN", "")
        )
        self.headers = {"Authorization": f"Bearer {self.api_token}"}
        self._host_failover = HostFailoverState(
            primary=resolved_server_host,
            fallback=resolved_fallback_host,
            threshold=2,
            default_scheme="http",
        )
        logger.info(
            "[CompetitionMCP] 初始化: primary=%s fallback=%s",
            safe_endpoint_label(self.server_host),
            safe_endpoint_label(self.fallback_server_host) or "<disabled>",
        )

    @property
    def active_server_host(self) -> str:
        return self._host_failover.snapshot().active

    @property
    def fallback_server_host(self) -> str:
        return self._host_failover.snapshot().fallback

    def describe_transport(self) -> Dict[str, Any]:
        snapshot = self._host_failover.snapshot()
        return {
            "protocol": "mcp-streamable-http",
            "auth_mode": "Authorization: Bearer",
            "primary": snapshot.primary,
            "fallback": snapshot.fallback,
            "active": snapshot.active,
            "failure_streak": snapshot.failure_streak,
            "switched": snapshot.switched,
            "mcp_url": self._build_mcp_url(snapshot.active),
        }

    def _build_mcp_url(self, server_host: str) -> str:
        normalized = normalize_host_url(server_host, default_scheme="http")
        return f"{normalized}/mcp" if normalized else ""

    def _ensure_config(self) -> None:
        if not self.server_host:
            raise CompetitionMCPError("COMPETITION_SERVER_HOST 未配置，无法访问主赛场 MCP")
        if not self.api_token:
            raise CompetitionMCPError("AGENT_TOKEN 未配置，无法访问主赛场 MCP")
        if ClientSession is None or streamablehttp_client is None:
            raise CompetitionMCPError("未安装 mcp SDK，无法接入主赛场 MCP")

    def _record_host_success(self, host: str) -> None:
        self._host_failover.record_success(host)

    def _record_host_failure(self, host: str, reason: str) -> bool:
        snapshot, switched = self._host_failover.record_failure(host)
        if not snapshot.fallback or normalize_host_url(host) != snapshot.primary:
            return False
        logger.warning(
            "[CompetitionMCP] 主域名访问失败 (%s/%s): host=%s reason=%s",
            snapshot.failure_streak,
            snapshot.threshold,
            safe_endpoint_label(snapshot.primary),
            reason,
        )
        if switched:
            logger.warning(
                "[CompetitionMCP] 已切换到主赛场内网回退入口: primary=%s fallback=%s",
                safe_endpoint_label(snapshot.primary),
                safe_endpoint_label(snapshot.fallback),
            )
        return switched

    @staticmethod
    def _extract_mcp_text(result: Any) -> str:
        parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _classify_mcp_exception(exc: Exception) -> CompetitionMCPError:
        text = str(exc or "").strip()
        lowered = text.lower()
        if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden")):
            return CompetitionMCPAuthError(text or "MCP 认证失败")
        if any(
            token in lowered
            for token in (
                "404",
                "408",
                "429",
                "500",
                "502",
                "503",
                "504",
                "connection refused",
                "timed out",
                "timeout",
                "not found",
                "non-json",
                "html",
            )
        ):
            return CompetitionMCPTransportError(text or "MCP 入口不可用")
        return CompetitionMCPError(text or "MCP 调用失败")

    async def _call_tool_with_host(
        self,
        host: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        self._ensure_config()
        mcp_url = self._build_mcp_url(host)
        try:
            async with streamablehttp_client(
                mcp_url,
                headers=self.headers,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments or {})
        except Exception as exc:
            raise self._classify_mcp_exception(exc) from exc

        text = self._extract_mcp_text(result)
        if getattr(result, "isError", False):
            error = self._classify_mcp_exception(Exception(text or f"MCP 工具 {tool_name} 调用失败"))
            raise error
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        self._ensure_config()
        host = self.active_server_host or self.server_host
        try:
            payload = await self._call_tool_with_host(host, tool_name, arguments or {})
            self._record_host_success(host)
            return payload
        except CompetitionMCPTransportError as exc:
            if self._record_host_failure(host, str(exc)):
                payload = await self._call_tool_with_host(
                    self.active_server_host,
                    tool_name,
                    arguments or {},
                )
                self._record_host_success(self.active_server_host)
                return payload
            raise

    async def probe_health(self) -> Dict[str, Any]:
        return await self.call_tool("list_challenges", {})


# ─── 全局单例 ───

_api_client: Optional[CompetitionAPIClient] = None
_competition_mcp_client: Optional[CompetitionMCPClient] = None


def get_api_client() -> CompetitionAPIClient:
    global _api_client
    if _api_client is None:
        _api_client = CompetitionAPIClient()
    return _api_client


def set_api_client(client: CompetitionAPIClient):
    """注入全局 API 客户端（用于主流程与工具层共享同一连接配置）。"""
    global _api_client
    _api_client = client


def get_competition_mcp_client() -> CompetitionMCPClient:
    global _competition_mcp_client
    if _competition_mcp_client is None:
        _competition_mcp_client = CompetitionMCPClient()
    return _competition_mcp_client


def set_competition_mcp_client(client: CompetitionMCPClient):
    """注入全局主赛场 MCP 客户端。"""
    global _competition_mcp_client
    _competition_mcp_client = client


def initialize_competition_mcp(
    server_host: str = "",
    api_token: str = "",
    server_host_fallback: str = "",
) -> CompetitionMCPClient:
    client = CompetitionMCPClient(
        server_host=server_host,
        api_token=api_token,
        server_host_fallback=server_host_fallback,
    )
    set_competition_mcp_client(client)
    return client


async def run_platform_api_io(func, *args, **kwargs):
    """将同步平台 API 调用隔离到专用线程池，避免占用默认 executor。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _PLATFORM_API_EXECUTOR,
        partial(func, *args, **kwargs),
    )


# ─── LangChain 工具 (对齐官方 API) ───


def _list_challenges_impl() -> str:
    """
    获取当前关卡及之前关卡的赛题列表。

    返回关卡等级、赛题信息（code, 难度, 分值, 状态, 入口地址等）。
    """
    try:
        client = get_api_client()
        data = client.get_challenges()
        challenges = data.get("challenges", [])

        formatted = []
        for c in challenges:
            entry = {
                "code": c.get("code"),
                "title": c.get("title", ""),
                "difficulty": c.get("difficulty"),
                "level": c.get("level"),
                "total_score": c.get("total_score"),
                "got_score": c.get("total_got_score", 0),
                "flags": f"{c.get('flag_got_count', 0)}/{c.get('flag_count', '?')}",
                "instance": c.get("instance_status", "stopped"),
            }
            # 仅运行中的实例才有入口
            if c.get("entrypoint"):
                entry["entrypoint"] = c["entrypoint"]
            formatted.append(entry)

        return json.dumps(
            {
                "current_level": data.get("current_level"),
                "total_challenges": data.get("total_challenges"),
                "solved_challenges": data.get("solved_challenges"),
                "challenges": formatted,
            },
            ensure_ascii=False,
            indent=2,
        )
    except (APIError, Exception) as e:
        return f"获取题目失败: {e}"


def _start_challenge_impl(challenge_code: str) -> str:
    """
    启动指定赛题的容器实例，获取攻击入口地址。

    ⚠️ 每队同时最多运行 3 个赛题实例。超出时需先停止其他实例。
    必须先启动实例才能对目标进行渗透测试。

    Args:
        challenge_code: 赛题唯一标识 (code)
    """
    try:
        client = get_api_client()
        result = client.start_challenge(challenge_code)
        data = result.get("data", "")
        msg = result.get("message", "")

        # 判断已完成的情况
        if isinstance(data, dict) and data.get("already_completed"):
            return f"ℹ️ 赛题 {challenge_code} 已全部完成，无需启动。"

        # 正常启动 — data 是入口地址列表
        if isinstance(data, list):
            entries = ", ".join(data)
            return f"✅ 实例启动成功！\n入口地址: {entries}\n\n立即开始对目标进行渗透测试。"
        else:
            return f"✅ {msg}\n详情: {data}"
    except APIError as e:
        return f"启动实例失败: {e}"


def _stop_challenge_impl(challenge_code: str) -> str:
    """
    停止指定赛题的容器实例，释放资源。

    完成赛题后应停止实例，以便启动其他赛题。

    Args:
        challenge_code: 赛题唯一标识 (code)
    """
    try:
        client = get_api_client()
        result = client.stop_challenge(challenge_code)
        return f"✅ 赛题 {challenge_code} 实例已停止。"
    except APIError as e:
        return f"停止实例失败: {e}"


@tool("list_challenges")
def list_challenges() -> str:
    """官方主赛场 MCP/工具名：获取当前关卡及之前关卡的赛题列表。"""
    return _list_challenges_impl()


@tool("get_challenge_list")
def get_challenge_list() -> str:
    """兼容旧工具名：获取当前关卡及之前关卡的赛题列表。"""
    return _list_challenges_impl()


@tool("start_challenge")
def start_challenge(code: str) -> str:
    """官方主赛场 MCP/工具名：启动指定赛题实例。"""
    return _start_challenge_impl(code)


@tool("start_challenge_instance")
def start_challenge_instance(challenge_code: str) -> str:
    """兼容旧工具名：启动指定赛题实例。"""
    return _start_challenge_impl(challenge_code)


@tool("stop_challenge")
def stop_challenge(code: str) -> str:
    """官方主赛场 MCP/工具名：停止指定赛题实例。"""
    return _stop_challenge_impl(code)


@tool("stop_challenge_instance")
def stop_challenge_instance(challenge_code: str) -> str:
    """兼容旧工具名：停止指定赛题实例。"""
    return _stop_challenge_impl(challenge_code)


@tool
def submit_flag(challenge_code: str, flag: str) -> str:
    """
    提交赛题答案 (flag)。

    ⚠️ 重要：
    - FLAG 格式: flag{...}
    - 赛题实例必须处于运行状态
    - 支持多 Flag 得分点，每个 Flag 只能得分一次
    - 提交正确后可能触发闯关升级解锁新关卡

    Args:
        challenge_code: 赛题唯一标识 (code)
        flag: 完整的 flag 值 (如 "flag{xxx}")
    """
    # 格式验证
    flag = flag.strip()
    if not flag:
        return "❌ FLAG 不能为空"
    if not flag.startswith("flag{") or not flag.endswith("}"):
        return f"❌ FLAG 格式错误，必须是 flag{{...}} 格式。当前: {flag}"
    if _flag_submit_key(challenge_code, flag) in _SUBMITTED_FLAGS_GLOBAL:
        return f"⚠️ 该 FLAG 已在当前进程提交过，跳过重复提交: {flag}"

    try:
        client = get_api_client()
        data, submit_note = _submit_answer_with_instance_recovery(client, challenge_code, flag)
        correct = data.get("correct", False)
        message = data.get("message", "")
        flag_count = data.get("flag_count", "?")
        flag_got = data.get("flag_got_count", 0)

        _SUBMITTED_FLAGS_GLOBAL.add(_flag_submit_key(challenge_code, flag))
        if correct:
            result = f"🎉 {message}\nFlag 进度: {flag_got}/{flag_count}"
            if submit_note:
                result = f"{submit_note}\n{result}"
            if "解锁新的关卡" in message:
                result += "\n\n🔓 恭喜！已解锁新关卡！请查看赛题列表。"
            return result
        else:
            result = (
                f"❌ {message}\n提交的 FLAG: {flag}\nFlag 进度: {flag_got}/{flag_count}"
            )
            if submit_note:
                result = f"{submit_note}\n{result}"
            return result
    except FlagRescueNotice as notice:
        return str(notice)
    except APIError as e:
        return f"提交失败: {e}"


# ─── 导出 ───

COMPETITION_TOOLS = [
    list_challenges,
    start_challenge,
    stop_challenge,
    get_challenge_list,
    start_challenge_instance,
    stop_challenge_instance,
    submit_flag,
]


def get_competition_tools() -> List:
    return COMPETITION_TOOLS


def get_competition_tools_for_challenge(challenge_code: str) -> List:
    """
    为单题任务生成仅作用于当前 challenge 的平台工具。

    这样主攻手在当前模块内只会提交当前题目，避免串题。
    """
    code = (challenge_code or "").strip()
    if not code:
        return []

    tools: List = []

    _submitted_flags: set[str] = set()

    @tool("submit_flag")
    async def scoped_submit_flag(flag: str) -> str:
        """提交当前题目的答案。该工具已自动绑定到当前模块，无需传 challenge_code。"""
        normalized = (flag or "").strip()
        if not normalized:
            return "❌ FLAG 不能为空"
        if not normalized.startswith("flag{") or not normalized.endswith("}"):
            return f"❌ FLAG 格式错误，必须是 flag{{...}} 格式。当前: {normalized}"
        if normalized in _submitted_flags:
            return f"⚠️ 该 FLAG 本轮已提交过，跳过重复提交: {normalized}"
        if _flag_submit_key(code, normalized) in _SUBMITTED_FLAGS_GLOBAL:
            return f"⚠️ 该 FLAG 已在当前进程提交过，跳过重复提交: {normalized}"

        try:
            client = get_api_client()
            data, submit_note = await run_platform_api_io(
                _submit_answer_with_instance_recovery,
                client,
                code,
                normalized,
            )
            correct = data.get("correct", False)
            message = data.get("message", "")
            flag_count = data.get("flag_count", "?")
            flag_got = data.get("flag_got_count", 0)

            _submitted_flags.add(normalized)
            _SUBMITTED_FLAGS_GLOBAL.add(_flag_submit_key(code, normalized))
            if correct:
                result = f"🎉 {message}\nFlag 进度: {flag_got}/{flag_count}"
                if submit_note:
                    result = f"{submit_note}\n{result}"
                if "解锁新的关卡" in message:
                    result += "\n\n🔓 恭喜！已解锁新关卡！请查看赛题列表。"
                return result
            result = (
                f"❌ {message}\n提交的 FLAG: {normalized}\n"
                f"Flag 进度: {flag_got}/{flag_count}"
            )
            if submit_note:
                result = f"{submit_note}\n{result}"
            return result
        except FlagRescueNotice as notice:
            return str(notice)
        except APIError as e:
            return f"提交失败: {e}"

    tools.append(scoped_submit_flag)
    return tools
