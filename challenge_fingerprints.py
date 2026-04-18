"""
主战场产品/组件指纹识别
======================
不要依赖题目标题猜框架，而是根据运行时页面、响应头、Banner 和默认路由识别真实考点。
"""

from __future__ import annotations

import re
from typing import Iterable


_PRODUCT_FINGERPRINT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "1Panel",
        (
            r"\b1panel\b",
            r"/1panel\b",
            r"1panel[-_/ ]?(?:login|app|panel)?",
            r"\bfit2cloud\b",
            r"\bpanel_client\b",
            r"/api/v1/hosts/command/search\b",
            r"/api/v2/hosts/terminal\b",
        ),
    ),
    (
        "ComfyUI Manager",
        (
            r"comfyui[-_/ ]?manager",
            r"\bcomfyui\b",
            r"custom[-_/ ]nodes[-_/ ]manager",
        ),
    ),
    (
        "Dify",
        (
            r"\bdify\b",
            r"langgenius",
        ),
    ),
    (
        "GeoServer",
        (
            r"\bgeoserver\b",
            r"\bgeowebcache\b",
            r"/geoserver\b",
            r"/wms\b",
            r"/wfs\b",
        ),
    ),
    (
        "Gradio",
        (
            r"\bgradio\b",
            r"__gradio__",
            r"/gradio_api\b",
            r"/queue/join\b",
        ),
    ),
    (
        "HugeGraph Gremlin RCE",
        (
            r"\bhugegraph\b",
            r"\bgremlin\b",
        ),
    ),
    (
        "GNU InetUtils Telnetd",
        (
            r"gnu\s+inetutils",
            r"\btelnetd\b",
            r"\btelnet\b",
        ),
    ),
    (
        "Langflow",
        (
            r"\blangflow\b",
        ),
    ),
    (
        "Nacos",
        (
            r"\bnacos\b",
            r"/nacos\b",
            r"nacos[-_/ ]?(?:console|server)?",
        ),
    ),
    (
        "OFBiz",
        (
            r"\bofbiz\b",
            r"apache\s+ofbiz",
            r"/webtools\b",
        ),
    ),
)

_PRODUCT_ATTACK_HINTS: dict[str, str] = {
    "1Panel": "优先检查登录后 API、文件管理、终端执行、容器/插件能力。",
    "ComfyUI Manager": "优先检查自定义节点/插件安装、工作流导入、文件写入与命令执行。",
    "Dify": "优先检查插件/工具调用、工作流配置、未授权 API、SSRF 与文件读取。",
    "GeoServer": "优先检查 WFS/WMS/REST 接口、任意文件读写和已知 RCE 链。",
    "Gradio": "优先检查 /config、/queue/join、文件读取、推理/接口调用与组件暴露。",
    "HugeGraph Gremlin RCE": "优先检查 Gremlin 查询执行、脚本注入与远程命令执行。",
    "GNU InetUtils Telnetd": "优先检查 telnet 服务暴露、弱认证、默认配置与命令执行。",
    "Langflow": "优先检查工作流导入、组件参数注入、文件读取和命令执行。",
    "Nacos": "优先检查未授权、默认口令、配置中心、控制台与历史 RCE/鉴权缺陷。",
    "OFBiz": "优先检查 webtools、XMLRPC、模板/反序列化与已知 RCE 链。",
}


def detect_product_fingerprints(text: str | None) -> list[str]:
    normalized = str(text or "")
    if not normalized.strip():
        return []
    lowered = normalized.lower()
    labels: list[str] = []
    for label, patterns in _PRODUCT_FINGERPRINT_PATTERNS:
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns):
            labels.append(label)
    return labels


def fingerprint_attack_hints(labels: Iterable[str], limit: int = 4) -> list[str]:
    hints: list[str] = []
    for label in labels:
        hint = _PRODUCT_ATTACK_HINTS.get(str(label))
        if not hint:
            continue
        hints.append(f"{label}: {hint}")
        if len(hints) >= max(1, int(limit)):
            break
    return hints
