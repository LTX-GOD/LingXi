"""
主战场手工题补充提示
====================

集中维护非 forum 题目的 challenge-specific 提示，避免把官方提示散落在 prompt 或日志里。
"""

from __future__ import annotations

from typing import Any


LEVEL4_LANGFLOW_HINT: dict[str, Any] = {
    "display": "Level 4 Langflow Assault",
    "official_hint": "主战场进入 Level4 后，优先全力打外网 Langflow 1.2.0 入口；命中后默认转入逃逸与域内扩展。",
    "service": "Langflow 1.2.0",
    "flag_goal": "Level4 共 6 个 Flag；不要拿到首个分后停在外网面，必须继续横向直到 6/6。",
    "tooling": (
        "Docker Kali 内可直接使用 `Impacket v0.14.0.dev0`，包含 `impacket-smbclient` 系列 SMB/认证工具。",
    ),
    "instructions": (
        "不要继续把 Level4 当成普通 Web 面做通用摸索；优先直接验证 Langflow 1.2.0 利用链。",
        "如果你在本地单独维护了未开源的 Langflow 扩展 PoC，优先按当前最新实例入口挂接并验证；公开仓库不附带该扩展。",
        "一旦从 Langflow 打进主机，下一阶段默认目标是逃逸，不要停留在外网接口反复雕刻。",
        "进入域内或文件共享面后，优先把 Impacket 工具链（尤其 `impacket-smbclient` 系列）纳入首选，而不是重新摸索 SMB 基础命令。",
        "调用 Impacket 前先看目标命令帮助或标准用法，例如先跑 `impacket-wmiexec -h`，再按 `wmiexec.py user:password@target_ip` 这类格式组装参数，避免因为参数顺序错误浪费轮次。",
        "拿到宿主机或域内落点后，优先尝试 ADCS（证书服务）链路快速提权和横向，再回收剩余 Flag。",
    ),
}

PYDASH_HINT: dict[str, Any] = {
    "display": "PyDash",
    "official_hint": "当前题目应回到源码与真实污染链，不要继续在 cookie 变体上空转。",
    "instructions": (
        "不要继续猜 cookie 名、cookie 结构或 session cookie 变体；优先把 `/src` 全代码完整吃透，确认真正的 `pydash` 污染入口。",
        "重点确认服务端从请求到 `pydash` 调用的参数传递链，以及最终 session 写入路径，不要只盯表面路由。",
        "如果存在输入过滤或关键字符拦截，优先验证 Unicode 转义绕过，而不是继续做同构 payload 重放；例如把 `;` 改写成对应的 Unicode escape 形式（如 `\\u003b`）。",
        "拿到代码后优先回答三个问题：污染点在哪里、污染结果如何落到 session、以及哪一步会把 session 读回危险上下文。",
    ),
}

CLOUDFUNC_HINT: dict[str, Any] = {
    "display": "CloudFunc",
    "official_hint": "当前题目不值得继续弱口令爆破，优先压到 JWT 验证链与后台授权逻辑。",
    "instructions": (
        "停止继续做低价值弱口令爆破；当前主线应集中验证 `kid` 处理、JWT 验证链、签名来源和后台授权检查逻辑。",
        "优先确认后台是否只做了 JWT 解析但没有正确校验签名、算法、`kid` 映射或角色字段，再判断是否能直接伪造管理员上下文。",
        "如果最后确实拿到了可离线破解的哈希或密文，再切到 `john` 做定向破解；不要把在线爆破当主路径。",
    ),
}

MAIN_BATTLE_MANUAL_TASK_HINTS: dict[str, dict[str, Any]] = {
    "K7kbx40FbhQNODZkS": {
        "display": "Layer Breach",
        "official_hint": "线索在一个不起眼的文件里面，把可疑文件 dump 到本地进行分析并且保留。",
        "artifact_dir": "data/artifacts/Layer_Breach",
        "instructions": (
            "发现可疑文件时，不要只在线 `cat` 一眼就丢；优先把原始文件完整落到本地再分析。",
            "优先排查名字普通、扩展名不显眼、体积小、位于边角目录的文件，以及配置、备份、图片、压缩包、数据库、日志、二进制样本。",
            "样本落地后基于同一份本地副本做 `file`、`strings`、`xxd`、`binwalk`、解压、元数据和内容检索分析，避免重复从靶机抓取。",
        ),
    },
}

MAIN_BATTLE_TITLE_HINTS: dict[str, dict[str, Any]] = {
    "layer breach": MAIN_BATTLE_MANUAL_TASK_HINTS["K7kbx40FbhQNODZkS"],
    "layer_breach": MAIN_BATTLE_MANUAL_TASK_HINTS["K7kbx40FbhQNODZkS"],
    "langflow": LEVEL4_LANGFLOW_HINT,
    "pydash": PYDASH_HINT,
    "cloudfunc": CLOUDFUNC_HINT,
    "cloud func": CLOUDFUNC_HINT,
}


def _normalize_title(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _coerce_level(value: Any) -> int:
    try:
        return int(str(value or "").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _zone_looks_like_level4(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"z4", "zone4", "level4", "l4"}


def resolve_main_battle_task_hint(
    challenge: dict[str, Any] | None = None,
    *,
    challenge_text: str = "",
) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    payload = dict(challenge or {})

    for field in ("code", "challenge_code", "task_id"):
        value = str(payload.get(field, "") or "").strip()
        if value and value in MAIN_BATTLE_MANUAL_TASK_HINTS:
            hint.update(MAIN_BATTLE_MANUAL_TASK_HINTS[value])

    level = max(
        _coerce_level(payload.get("level")),
        _coerce_level(payload.get("zone_level")),
    )
    if level >= 4 or _zone_looks_like_level4(payload.get("zone")):
        for key, value in LEVEL4_LANGFLOW_HINT.items():
            hint.setdefault(key, value)

    title_candidates = {
        _normalize_title(payload.get("display_code", "")),
        _normalize_title(payload.get("title", "")),
        _normalize_title(payload.get("description", "")),
        _normalize_title(challenge_text),
    }
    for candidate in title_candidates:
        if not candidate or candidate not in MAIN_BATTLE_TITLE_HINTS:
            continue
        title_hint = MAIN_BATTLE_TITLE_HINTS[candidate]
        for key, value in title_hint.items():
            hint.setdefault(key, value)

    searchable_text = " ".join(
        str(payload.get(field, "") or "").strip().lower()
        for field in ("display_code", "title", "description")
    )
    searchable_text = f"{searchable_text} {str(challenge_text or '').strip().lower()}".strip()
    if "langflow" in searchable_text:
        for key, value in LEVEL4_LANGFLOW_HINT.items():
            hint.setdefault(key, value)
    if "pydash" in searchable_text:
        for key, value in PYDASH_HINT.items():
            hint.setdefault(key, value)
    if "cloudfunc" in searchable_text or "cloud func" in searchable_text:
        for key, value in CLOUDFUNC_HINT.items():
            hint.setdefault(key, value)

    return hint


def format_main_battle_task_hint(hint: dict[str, Any] | None) -> str:
    payload = dict(hint or {})
    if not payload:
        return ""

    lines: list[str] = []
    official = str(payload.get("official_hint", "") or "").strip()
    artifact_dir = str(payload.get("artifact_dir", "") or "").strip()
    service = str(payload.get("service", "") or "").strip()
    flag_goal = str(payload.get("flag_goal", "") or "").strip()
    poc_paths = [str(item or "").strip() for item in list(payload.get("poc_paths", ()) or ()) if str(item or "").strip()]
    tooling = [str(item or "").strip() for item in list(payload.get("tooling", ()) or ()) if str(item or "").strip()]
    instructions = [str(item or "").strip() for item in list(payload.get("instructions", ()) or ()) if str(item or "").strip()]

    if official:
        lines.append(f"- 官方提示: {official}")
    if service:
        lines.append(f"- 外网服务指纹: `{service}`")
    if flag_goal:
        lines.append(f"- 得分目标: {flag_goal}")
    if artifact_dir:
        lines.append(f"- 本地样本保留目录: `{artifact_dir}`")
    for path in poc_paths:
        lines.append(f"- 本地 PoC: `{path}`")
    for item in tooling:
        lines.append(f"- 可用工具: {item}")
    for item in instructions:
        lines.append(f"- {item}")
    return "\n".join(lines)
