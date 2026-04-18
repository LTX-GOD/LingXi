"""
Claude Code SDK Runner
======================
替代 LangGraph build_graph() + solver.py 的核心执行层。

架构映射:
  LangGraph StateGraph        → query() agentic loop
  main_agent_node 守卫         → can_use_tool callback
  advisor_node                → 独立 query() 调用
  ToolNode                    → MCP servers
  AgentState                  → RunnerState dataclass
"""
import asyncio
import inspect
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from claude_code_sdk import ClaudeCodeOptions
from claude_code_sdk.client import ClaudeSDKClient
from claude_code_sdk.types import (
    AssistantMessage,
    SystemMessage,
    UserMessage,
    ResultMessage,
    ToolUseBlock,
    ToolResultBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from agent.prompts import (
    MAIN_BATTLE_AGENT_PROMPT,
    FORUM_AGENT_PROMPT,
    MAIN_BATTLE_ADVISOR_PROMPT,
    FORUM_ADVISOR_PROMPT,
    render_prompt_template,
)
from agent.main_battle_progress import apply_main_battle_score_progress

logger = logging.getLogger(__name__)


def _resolve_advisor_timeout_seconds() -> float:
    try:
        configured = float(os.getenv("LINGXI_ADVISOR_TIMEOUT", "100") or 100)
    except (TypeError, ValueError):
        configured = 100.0
    return max(3.0, configured)


def _sdk_startup_retry_backoff_seconds(attempt: int) -> float:
    normalized_attempt = max(1, int(attempt or 1))
    return min(30.0, 3.0 * (2 ** (normalized_attempt - 1)))


_ADVISOR_FALLBACK_MESSAGE = "顾问暂时不可用，按当前证据继续推进。"
_ADVISOR_EXECUTION_PREFIX = "执行顾问建议:"
_ADVISOR_CALL_TIMEOUT_SECONDS = _resolve_advisor_timeout_seconds()
_SDK_CLOSE_SCOPE_MARKERS = (
    "attempted to exit a cancel scope",
    "cancel scope",
    "generatorexit",
    "async_generator_athrow",
    "_exceptions",
)
_SDK_CONNECT_TIMEOUT_SECONDS = max(5.0, float(os.getenv("LINGXI_SDK_CONNECT_TIMEOUT", "20") or 20))
_SDK_INITIAL_QUERY_TIMEOUT_SECONDS = max(3.0, float(os.getenv("LINGXI_SDK_QUERY_TIMEOUT", "15") or 15))
_SDK_FIRST_RESPONSE_TIMEOUT_SECONDS = max(5.0, float(os.getenv("LINGXI_SDK_FIRST_RESPONSE_TIMEOUT", "45") or 45))
_SDK_IDLE_RESPONSE_TIMEOUT_SECONDS = max(5.0, float(os.getenv("LINGXI_SDK_IDLE_RESPONSE_TIMEOUT", "120") or 120))
_SDK_CLOSE_TIMEOUT_SECONDS = max(1.0, float(os.getenv("LINGXI_SDK_CLOSE_TIMEOUT", "5") or 5))
_SDK_STARTUP_RETRY_ATTEMPTS = max(1, int(os.getenv("LINGXI_SDK_STARTUP_RETRY_ATTEMPTS", "2") or 2))
_ACTIVE_SDK_HANDLES_LOCK = threading.RLock()
_ACTIVE_SDK_HANDLES: dict[int, tuple[Any, str, str]] = {}
_SDK_RUNTIME_ENV_LOADED = False
_SDK_SESSION_SEMAPHORE: asyncio.Semaphore | None = None
_SDK_SESSION_SEMAPHORE_LIMIT = 0
_SDK_SESSION_SEMAPHORE_LOCK = threading.Lock()
_SDK_SYSTEM_FAILURE_TOKENS = (
    "api_retry",
    "server_error",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "502",
    "503",
    "504",
    "524",
)

# ─── 守卫常量（与 graph.py 保持一致） ───
_FORUM2_KEY_DISCLOSURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?is)\bkey\s*[abc]\s*[:=：]\s*[`\"']?[a-z0-9_-]{2,}"), "检测到显式 Key 值赋值"),
    (re.compile(r"(?is)(前\s*(?:4|四)\s*位|前缀)\s*(?:是|为|[:：=])\s*[`\"']?[a-z0-9_-]{2,}"), "检测到前缀泄露"),
    (re.compile(r"(?is)(我这边|我方|我们|我的|我有|我持有)[^\n]{0,24}\bkey\s*[abc]\b"), "检测到我方 Key 持有信息外泄"),
    (re.compile(r"(?is)(我这边|我方|我们|我会|我们会|我将|我们将)[^\n]{0,12}(同步给你|发给你|给你|提供给你|回你)[^\n]{0,16}(完整|全部|前缀|前4位|前四位|key)"), "检测到承诺向外发送我方 Key 信息"),
]
_EARLY_HEAVY_SCAN_PATTERNS = (
    re.compile(r"\bnmap\b.*(?:\b-sC\b|\b-sV\b|\b--min-rate\b|\b-p\s*1-65535\b)", re.IGNORECASE),
    re.compile(r"\bgobuster\s+dir\b", re.IGNORECASE),
    re.compile(r"\bffuf\b", re.IGNORECASE),
    re.compile(r"\bferoxbuster\b", re.IGNORECASE),
)
_MSF_COMMAND_PATTERNS = (
    re.compile(r"\bmsfconsole\b", re.IGNORECASE),
    re.compile(r"\bmsfvenom\b", re.IGNORECASE),
    re.compile(r"\bmsfrpcd\b", re.IGNORECASE),
    re.compile(r"\bmsfdb\b", re.IGNORECASE),
    re.compile(r"\bmetasploit\b", re.IGNORECASE),
)
_FORUM2_OUTBOUND_TOOLS = {
    "send_direct_message", "create_post", "create_comment",
    "forum_send_direct_message", "forum_create_post", "forum_create_comment",
}
_AUTH_SURFACE_MARKERS = ("/token", "openapi.json", "/docs", "authorization", "bearer", "jwt", "access_token", "set-cookie", "www-authenticate", "auth-jwt")
_HTTP_SURFACE_MARKERS = ("http/1.1", "title:", "links:", "set-cookie", "content-type", "server:", "location:", "/login", "/docs", "/token", "openapi.json", "login-form")
_HTTP_BASELINE_MARKERS = ("/docs", "openapi.json", "/login", "/token", "robots.txt", ".git/head", "index.php", "/ping")
_TOOL_PROXY_BIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "tool-proxy-bin",
)


def _clip_log_text(text: Any, limit: int = 900) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _register_sdk_handle(handle: Any, *, label: str, challenge_code: str) -> None:
    if handle is None:
        return
    with _ACTIVE_SDK_HANDLES_LOCK:
        _ACTIVE_SDK_HANDLES[id(handle)] = (handle, label, challenge_code)


def _unregister_sdk_handle(handle: Any) -> None:
    if handle is None:
        return
    with _ACTIVE_SDK_HANDLES_LOCK:
        _ACTIVE_SDK_HANDLES.pop(id(handle), None)


async def _await_sdk_close(
    close_result: Any,
    *,
    label: str,
    challenge_code: str,
    timeout: float,
    ignore_error_markers: tuple[str, ...] = (),
) -> str:
    try:
        if inspect.isawaitable(close_result):
            async with asyncio.timeout(timeout):
                await close_result
        return "closed"
    except TimeoutError:
        logger.warning(
            "[SDK] 关闭超时: challenge=%s handle=%s timeout=%.1fs",
            challenge_code,
            label,
            timeout,
        )
        return "timeout"
    except Exception as exc:
        message = str(exc).lower()
        if ignore_error_markers and any(marker in message for marker in ignore_error_markers):
            logger.warning(
                "[SDK] 忽略关闭期异步清理异常: challenge=%s handle=%s err=%s",
                challenge_code,
                label,
                exc,
            )
            return "ignored"
        logger.warning(
            "[SDK] 关闭失败: challenge=%s handle=%s err=%s",
            challenge_code,
            label,
            exc,
        )
        return "error"


async def _close_sdk_handle(
    handle: Any,
    *,
    label: str,
    challenge_code: str,
    timeout: float,
) -> str:
    if handle is None:
        return "skipped"
    if label == "client":
        return await _await_sdk_close(
            handle.__aexit__(None, None, None),
            label=label,
            challenge_code=challenge_code,
            timeout=timeout,
            ignore_error_markers=_SDK_CLOSE_SCOPE_MARKERS,
        )

    aclose = getattr(handle, "aclose", None)
    if callable(aclose):
        return await _await_sdk_close(
            aclose(),
            label=label,
            challenge_code=challenge_code,
            timeout=timeout,
            ignore_error_markers=_SDK_CLOSE_SCOPE_MARKERS,
        )
    return "skipped"


async def _iterate_with_timeouts(
    stream: Any,
    *,
    first_timeout: float,
    idle_timeout: float,
    counts_as_progress: Optional[Callable[[Any], bool]] = None,
) -> Any:
    iterator = stream.__aiter__()
    received_count = 0
    while True:
        timeout = first_timeout if received_count == 0 else idle_timeout
        try:
            async with asyncio.timeout(timeout):
                message = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            phase = "first_response" if received_count == 0 else "idle_response"
            raise TimeoutError(f"{phase}_timeout:{timeout:.1f}s") from exc
        if counts_as_progress is None or counts_as_progress(message):
            received_count += 1
        yield message


def _message_counts_as_progress(message: Any) -> bool:
    return not isinstance(message, SystemMessage)


def _system_message_failure_reason(message: SystemMessage) -> str:
    subtype = str(getattr(message, "subtype", "") or "").strip().lower()
    payload = getattr(message, "data", {}) or {}
    error = str(payload.get("error", "") or payload.get("message", "") or "").strip()
    status = str(payload.get("error_status", "") or payload.get("status", "") or "").strip()

    if subtype in {"api_retry", "error"}:
        parts = [part for part in (subtype, status, error) if part]
        return ":".join(parts) if parts else subtype

    signal_text = " ".join(
        str(payload.get(key, "") or "").strip().lower()
        for key in ("error", "message", "result")
        if str(payload.get(key, "") or "").strip()
    )
    if any(token in signal_text for token in _SDK_SYSTEM_FAILURE_TOKENS):
        parts = [part for part in (subtype or "system", status, error or _clip_log_text(signal_text, 160)) if part]
        return ":".join(parts)

    return ""


def _looks_like_sdk_startup_failure(error_text: str) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    return any(
        token in lowered
        for token in (
            "first_response_timeout",
            "api_retry",
            "server_error",
            "bad gateway",
            "gateway timeout",
            "timed out",
            "timeout",
            "connection refused",
            "connection reset",
            "502",
            "503",
            "504",
            "524",
        )
    )


def get_active_sdk_handle_count() -> int:
    with _ACTIVE_SDK_HANDLES_LOCK:
        return len(_ACTIVE_SDK_HANDLES)


async def shutdown_active_sdk_sessions(timeout: float = _SDK_CLOSE_TIMEOUT_SECONDS) -> dict[str, int]:
    with _ACTIVE_SDK_HANDLES_LOCK:
        snapshot = list(_ACTIVE_SDK_HANDLES.values())

    summary = {
        "attempted": len(snapshot),
        "closed": 0,
        "timed_out": 0,
        "failed": 0,
        "skipped": 0,
    }
    if not snapshot:
        return summary

    per_handle_timeout = max(1.0, float(timeout) / max(1, len(snapshot)))
    logger.info(
        "[SDK] 开始同步关闭活跃会话: handles=%s timeout=%.1fs per_handle=%.1fs",
        len(snapshot),
        timeout,
        per_handle_timeout,
    )
    results = await asyncio.gather(
        *[
            _close_sdk_handle(
                handle,
                label=label,
                challenge_code=challenge_code,
                timeout=per_handle_timeout,
            )
            for handle, label, challenge_code in snapshot
        ],
        return_exceptions=True,
    )

    for (handle, _, _), result in zip(snapshot, results):
        _unregister_sdk_handle(handle)
        if isinstance(result, Exception):
            summary["failed"] += 1
        elif result == "closed":
            summary["closed"] += 1
        elif result == "timeout":
            summary["timed_out"] += 1
        elif result == "skipped":
            summary["skipped"] += 1
        else:
            summary["failed"] += 1

    logger.info(
        "[SDK] 活跃会话关闭完成: attempted=%s closed=%s timed_out=%s failed=%s skipped=%s",
        summary["attempted"],
        summary["closed"],
        summary["timed_out"],
        summary["failed"],
        summary["skipped"],
    )
    return summary


def _bounded_append(items: list[str], value: str, *, limit: int) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        return
    items.append(normalized)
    if len(items) > limit:
        del items[:-limit]


def _summarize_knowledge_sources(context: str) -> str:
    lowered = str(context or "").lower()
    sources: list[str] = []
    if "主战场记忆" in context:
        sources.append("main_memory")
    if "论坛记忆" in context:
        sources.append("forum_memory")
    if "各大 ctf wp" in lowered or "external_writeup" in lowered or "external_hypothesis" in lowered:
        sources.append("external_wp")
    return ",".join(sources) if sources else "none"


@dataclass
class RunnerState:
    """轻量运行时状态（替代 AgentState TypedDict）"""
    challenge: dict
    is_forum: bool
    is_testenv: bool
    attempts: int = 0
    no_tool_rounds: int = 0
    is_finished: bool = False
    flag: Optional[str] = None
    seen_flags: list = field(default_factory=list)
    rejected_flags: list = field(default_factory=list)
    scored_flags: list = field(default_factory=list)
    flags_scored_count: int = 0
    expected_flag_count: int = 1
    action_history: list = field(default_factory=list)
    payload_history: list = field(default_factory=list)
    last_tool_name: Optional[str] = None
    consecutive_same_tool_calls: int = 0
    consecutive_failures: int = 0
    consecutive_llm_errors: int = 0
    last_llm_error: Optional[str] = None
    current_strategy: Optional[str] = None
    advisor_directive_pending: str = ""
    decision_history: list = field(default_factory=list)
    advisor_history: list = field(default_factory=list)
    knowledge_history: list = field(default_factory=list)
    advisor_call_count: int = 0
    knowledge_call_count: int = 0
    system_prompt_excerpt: str = ""
    initial_prompt_excerpt: str = ""
    memory_context_excerpt: str = ""
    skill_context_excerpt: str = ""
    # 运行时配置
    max_turns: int = 70
    tool_loop_break_threshold: int = 20
    advisor_no_tool_threshold: int = 2
    advisor_consultation_interval: int = 0
    consecutive_failures_threshold: int = 3
    recon_info: str = ""
    zone_strategy: str = ""
    memory_context: str = ""
    skill_context: str = ""
    enabled_skills: list = field(default_factory=list)
    progress_snapshot: dict[str, Any] | None = None


def _has_auth_surface(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"\beyj[a-z0-9_-]{10,}", lowered):
        return True
    return any(m in lowered for m in _AUTH_SURFACE_MARKERS)


def _has_http_surface(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in _HTTP_SURFACE_MARKERS)


def _has_consumed_baseline(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in _HTTP_BASELINE_MARKERS)


def _looks_like_complex_http_bash(command: str) -> bool:
    lowered = (command or "").lower()
    if "curl" not in lowered:
        return False
    markers = (
        "authorization:",
        "bearer ",
        "cookie:",
        "content-type: application/json",
        "--data",
        "--data-binary",
        "-d ",
        "{",
        "}",
    )
    quote_count = command.count("'") + command.count('"')
    return quote_count >= 4 and any(marker in lowered for marker in markers)


def _sync_progress_snapshot(state: RunnerState) -> None:
    if state.progress_snapshot is None:
        return
    state.progress_snapshot.update(
        {
            "attempts": int(state.attempts),
            "flag": state.flag or "",
            "flags_scored_count": int(state.flags_scored_count),
            "expected_flag_count": int(state.expected_flag_count),
            "scored_flags": list(state.scored_flags[-8:]),
            "action_history": list(state.action_history[-20:]),
            "payloads": list(state.payload_history[-8:]),
            "decision_history": list(state.decision_history[-12:]),
            "advisor_call_count": int(state.advisor_call_count),
            "advisor_history": list(state.advisor_history[-8:]),
            "knowledge_call_count": int(state.knowledge_call_count),
            "knowledge_history": list(state.knowledge_history[-8:]),
            "system_prompt_excerpt": state.system_prompt_excerpt,
            "initial_prompt_excerpt": state.initial_prompt_excerpt,
            "memory_context_excerpt": state.memory_context_excerpt,
            "skill_context_excerpt": state.skill_context_excerpt,
            "current_strategy": state.current_strategy or "",
            "advisor_directive_pending": state.advisor_directive_pending or "",
        }
    )


def _build_main_sdk_env() -> dict[str, str]:
    return _build_sdk_process_env(
        provider_env_var="MAIN_LLM_PROVIDER",
        default_provider="anthropic",
        sdk_model_env_var="SDK_MODEL",
        anthropic_base_url_env_var="ANTHROPIC_BASE_URL",
        anthropic_api_key_env_var="ANTHROPIC_API_KEY",
        anthropic_model_env_var="ANTHROPIC_MODEL",
    )


def _resolve_response_turn_budget(
    *,
    max_turns: int,
    advisor_no_tool_threshold: int,
    consecutive_failures_threshold: int,
    advisor_consultation_interval: int,
) -> int:
    candidates = [max(1, int(max_turns))]
    for value in (
        advisor_no_tool_threshold,
        consecutive_failures_threshold,
        advisor_consultation_interval,
    ):
        if int(value or 0) > 0:
            candidates.append(int(value))
    return max(1, min(candidates))


def _next_periodic_advisor_turn(
    *,
    advisor_consultation_interval: int,
    last_periodic_advisor_turn: int,
) -> int:
    interval = int(advisor_consultation_interval or 0)
    if interval <= 0:
        return 0
    return int(last_periodic_advisor_turn or 0) + interval


def _build_advisor_reasons(
    *,
    no_tool_rounds: int,
    advisor_no_tool_threshold: int,
    consecutive_failures: int,
    consecutive_failures_threshold: int,
    advisor_consultation_interval: int,
    total_turns: int,
    last_periodic_advisor_turn: int,
) -> list[str]:
    reasons: list[str] = []
    if no_tool_rounds >= advisor_no_tool_threshold:
        reasons.append(f"no_tool_rounds={no_tool_rounds}")
    if consecutive_failures >= consecutive_failures_threshold:
        reasons.append(f"consecutive_failures={consecutive_failures}")
    next_periodic_turn = _next_periodic_advisor_turn(
        advisor_consultation_interval=advisor_consultation_interval,
        last_periodic_advisor_turn=last_periodic_advisor_turn,
    )
    if (
        advisor_consultation_interval > 0
        and total_turns > 0
        and next_periodic_turn > 0
        and total_turns >= next_periodic_turn
    ):
        reasons.append(
            f"periodic_consultation(turn={next_periodic_turn},current={total_turns},interval={advisor_consultation_interval})"
        )
    return reasons


def _ensure_sdk_runtime_env_loaded() -> None:
    global _SDK_RUNTIME_ENV_LOADED
    if _SDK_RUNTIME_ENV_LOADED:
        return
    _SDK_RUNTIME_ENV_LOADED = True

    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.isfile(env_path):
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    try:
        load_dotenv(env_path, override=False)
    except Exception as exc:
        logger.debug("[SDK] 加载 .env 失败: %s", exc)


def _build_sdk_base_env() -> dict[str, str]:
    _ensure_sdk_runtime_env_loaded()

    current_path = os.environ.get("PATH", "")
    path_parts = [part for part in current_path.split(os.pathsep) if part]
    if os.path.isdir(_TOOL_PROXY_BIN_DIR) and _TOOL_PROXY_BIN_DIR not in path_parts:
        path_parts.insert(0, _TOOL_PROXY_BIN_DIR)
        os.environ["PATH"] = os.pathsep.join(path_parts)

    path_value = os.environ.get("PATH", "")
    return {"PATH": path_value} if path_value else {}


def _resolve_sdk_session_concurrency() -> int:
    raw_value = str(os.getenv("LINGXI_SDK_MAX_CONCURRENCY", "") or "").strip()
    if not raw_value:
        return 3
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 3


def _get_sdk_session_semaphore() -> tuple[asyncio.Semaphore, int]:
    global _SDK_SESSION_SEMAPHORE, _SDK_SESSION_SEMAPHORE_LIMIT
    limit = _resolve_sdk_session_concurrency()
    with _SDK_SESSION_SEMAPHORE_LOCK:
        if _SDK_SESSION_SEMAPHORE is None or _SDK_SESSION_SEMAPHORE_LIMIT != limit:
            _SDK_SESSION_SEMAPHORE = asyncio.Semaphore(limit)
            _SDK_SESSION_SEMAPHORE_LIMIT = limit
        return _SDK_SESSION_SEMAPHORE, limit


def _build_sdk_process_env(
    *,
    provider_env_var: str,
    default_provider: str,
    sdk_model_env_var: str,
    anthropic_base_url_env_var: str,
    anthropic_api_key_env_var: str,
    anthropic_model_env_var: str,
) -> dict[str, str]:
    env = _build_sdk_base_env()

    provider = str(os.getenv(provider_env_var, default_provider) or default_provider).strip().lower()
    sdk_model = str(os.getenv(sdk_model_env_var, "") or "").strip()
    if provider == "deepseek":
        base_url = str(os.getenv("DEEPSEEK_BASE_URL", "") or "").strip()
        api_key = str(os.getenv("DEEPSEEK_API_KEY", "") or "").strip()
        model = sdk_model or str(os.getenv("DEEPSEEK_MODEL", "") or "").strip()
    else:
        base_url = str(os.getenv(anthropic_base_url_env_var, "") or "").strip()
        api_key = str(os.getenv(anthropic_api_key_env_var, "") or "").strip()
        model = sdk_model or str(os.getenv(anthropic_model_env_var, "") or "").strip()

    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    if model:
        env["ANTHROPIC_MODEL"] = model
    return env


def _should_attach_sliver_mcp(challenge: dict) -> bool:
    if bool(challenge.get("forum_task", False)):
        return False
    if bool(challenge.get("sliver_task", False)):
        return True

    zone = str(challenge.get("zone", "") or "").strip().lower()
    if zone in {"z3", "z4"}:
        return True

    try:
        level = int(challenge.get("level", 0) or 0)
    except (TypeError, ValueError):
        level = 0
    return level >= 3


def _should_attach_kali_mcp(challenge: dict) -> bool:
    if bool(challenge.get("forum_task", False)):
        return False
    if bool(challenge.get("kali_task", False)):
        return True

    zone = str(challenge.get("zone", "") or "").strip().lower()
    if zone in {"z3", "z4"}:
        return True

    try:
        level = int(challenge.get("level", 0) or 0)
    except (TypeError, ValueError):
        level = 0
    return level >= 3


def _detect_forum2_key_disclosure(text: str) -> Optional[str]:
    for pattern, reason in _FORUM2_KEY_DISCLOSURE_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None


def _looks_like_heavy_scan(command: str) -> bool:
    return any(p.search(command or "") for p in _EARLY_HEAVY_SCAN_PATTERNS)


def _looks_like_msf_command(command: str) -> bool:
    return any(p.search(command or "") for p in _MSF_COMMAND_PATTERNS)


def _recent_msf_attempt_count(state: RunnerState) -> int:
    count = 0
    for item in list(state.payload_history or []):
        if _looks_like_msf_command(str(item or "")):
            count += 1
    return count


def _looks_like_auth_shell(command: str) -> bool:
    lowered = (command or "").lower()
    if "curl" not in lowered:
        return False
    if re.search(r"\b(token|cookie)\s*=", lowered):
        return True
    return any(m in lowered for m in ("authorization:", "bearer ", "cookie:", "--cookie", "access_token", "jwt"))


def _build_system_prompt(state: RunnerState) -> str:
    """构建系统提示（对应 graph.py main_agent_node 中的 system_prompt 构建逻辑）"""
    challenge = state.challenge
    challenge_info_parts = [
        f"- 题目代码: {challenge.get('code', '')}",
        f"- 难度: {challenge.get('difficulty', '')}",
        f"- 分值: {challenge.get('total_score', '')}",
        f"- 描述: {challenge.get('description', '')}",
    ]
    entrypoint = challenge.get("entrypoint") or []
    if entrypoint:
        challenge_info_parts.append(f"- 目标: {', '.join(entrypoint)}")
    challenge_info = "\n".join(challenge_info_parts)

    if state.flags_scored_count > 0 and state.flags_scored_count < state.expected_flag_count:
        challenge_info += (
            f"\n- 🚩 进度: 已收集 {state.flags_scored_count}/{state.expected_flag_count} 个 Flag。"
            "若已得分，切勿在当前外网入口反复浪费时间，必须向内网/更深层权限横向移动寻找剩余 Flag！"
        )

    recon_section = f"## 自动侦察结果\n{state.recon_info}" if state.recon_info else ""
    if state.zone_strategy:
        recon_section = f"{state.zone_strategy}\n\n{recon_section}"
    if state.memory_context:
        recon_section += f"\n\n## 记忆 (历史信息)\n{state.memory_context}"

    skill_section = (
        f"## 本地技能摘要（用于补充思路，不替代当前已验证路径）\n{state.skill_context}"
        if state.skill_context else ""
    )

    history_section = ""
    if state.action_history:
        recent = state.action_history[-15:] if state.is_forum else state.action_history[-12:]
        history_section = f"## 操作历史 (最近 {len(recent)} 条)\n" + "\n".join(recent)
    if state.current_strategy:
        history_section += (
            ("\n\n" if history_section else "")
            + "## 当前执行约束\n" + state.current_strategy
        )

    if state.is_testenv:
        recon_section += (
            "\n\n## 测试环境约束\n"
            "- 当前任务是手工测试环境，不连接比赛平台。\n"
            "- 你的目标是定位漏洞并提取 flag；一旦工具输出中出现明确 flag，即视为完成。\n"
            "- 不要尝试任何比赛平台实例管理或提交动作。"
        )
    elif state.is_forum:
        forum_module = int(challenge.get("forum_challenge_id", 0) or 0)
        recon_section += (
            "\n\n## 零界论坛赛道约束\n"
            f"- 当前任务只允许处理 forum-{forum_module} 这个论坛模块，不要切换到其他论坛题。\n"
            "- 不要做 nmap、gobuster、端口扫描或主战场实例操作。\n"
            "- 优先使用当前模块暴露的 forum_* 工具浏览帖子、评论、私信、智能体信息和挑战列表。\n"
            "- 如果找到 flag，只能用 `forum_submit_flag(flag)` 提交；该工具已自动绑定当前论坛题号。\n"
        )
    else:
        recon_section += (
            "\n\n## 当前模块约束\n"
            "- 当前任务只允许攻击本题入口地址，不得切换到其他 challenge code 或其他目标主机。\n"
            "- `submit_flag(flag)` 已自动绑定到当前模块。\n"
        )
        combined = f"{challenge_info}\n{state.recon_info or ''}\n{state.memory_context or ''}\n{' '.join(state.payload_history[-12:])}"
        if _has_auth_surface(combined):
            recon_section += (
                "\n## 认证快路径硬约束\n"
                "- 已出现 `/token` / `openapi.json` / JWT / Bearer / Cookie 信号。\n"
                "- 下一步禁止继续用 `execute_command` + shell 变量拼认证头。\n"
                "- 如果运行时已命中当前已接入的 Level2 PoC 组件，尤其 1Panel/Gradio/ComfyUI Manager，优先直接调用 `run_level2_cve_poc`，不要在 `execute_python` 里重写同一条利用链。\n"
                "- 只有当前组件没有现成 PoC，或确实缺少登录态需要先拿 cookie/token 时，才改用 `execute_python(requests.Session())` 完成登录、保存 cookie/token、访问受保护接口。\n"
            )

    prompt_template = FORUM_AGENT_PROMPT if state.is_forum else MAIN_BATTLE_AGENT_PROMPT
    return render_prompt_template(
        prompt_template,
        challenge_info=challenge_info,
        recon_section=recon_section,
        skill_section=skill_section,
        advisor_section="",
        history_section=history_section,
    )


def _build_advisor_sdk_env() -> dict[str, str]:
    """
    为 claude-code-sdk 的顾问子进程显式注入顾问凭据。
    Claude CLI / SDK 子进程读取的是通用 ANTHROPIC_* 变量，因此这里按顾问 provider
    把对应端点映射覆盖到子进程环境，避免顾问链路继续走全局 CLI 环境变量。
    """
    return _build_sdk_process_env(
        provider_env_var="ADVISOR_LLM_PROVIDER",
        default_provider="anthropic",
        sdk_model_env_var="SDK_ADVISOR_MODEL",
        anthropic_base_url_env_var="ADVISOR_ANTHROPIC_BASE_URL",
        anthropic_api_key_env_var="ADVISOR_ANTHROPIC_API_KEY",
        anthropic_model_env_var="ADVISOR_ANTHROPIC_MODEL",
    )


def _extract_langchain_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
            else:
                text = str(getattr(item, "text", "") or item or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _advisor_decision_is_acknowledged(decision_text: str) -> bool:
    normalized = str(decision_text or "").strip()
    if not normalized:
        return False
    return normalized.startswith(_ADVISOR_EXECUTION_PREFIX)


def _should_enforce_advisor_directive(suggestion: str) -> bool:
    normalized = str(suggestion or "").strip()
    return bool(normalized) and normalized != _ADVISOR_FALLBACK_MESSAGE


def _build_advisor_followup_prompt(suggestion: str) -> str:
    normalized = str(suggestion or "").strip() or "继续当前方向，但下一步必须做一次低成本高信号验证。"
    return (
        "【强制执行顾问指令】你刚刚收到顾问纠偏意见，这不是可选提示，而是下一步必须优先执行的约束。\n"
        "要求：\n"
        "1. 禁止继续沿用上一轮已被判定为低收益的路线。\n"
        f"2. 下一条助手回复必须先以 `{_ADVISOR_EXECUTION_PREFIX}` 开头，明确复述你将如何执行顾问建议。\n"
        "3. 紧接着立即调用一个能直接落实该建议的工具。\n"
        "4. 在完成这一步之前，不得进行大范围扫描、目录爆破、无关 fuzz 或切换到其他攻击方向。\n\n"
        f"顾问建议：\n{normalized}"
    )


def _make_can_use_tool(state: RunnerState):
    """
    构建 can_use_tool callback（替代 graph.py 中 main_agent_node 的守卫逻辑）。
    返回 PermissionResultAllow 或 PermissionResultDeny。
    """
    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # ── 工具循环熔断 ──
        threshold = state.tool_loop_break_threshold
        if tool_name == state.last_tool_name:
            state.consecutive_same_tool_calls += 1
        else:
            state.consecutive_same_tool_calls = 1
            state.last_tool_name = tool_name

        if state.consecutive_same_tool_calls >= threshold:
            msg = (
                f"【强制切换】已连续 {state.consecutive_same_tool_calls} 轮使用 `{tool_name}`，禁止继续调用。"
                "下一步必须切换到本质不同的工具或攻击路径，不得在同一工具上微调参数。"
            )
            state.current_strategy = msg
            _sync_progress_snapshot(state)
            logger.warning("[Guard] 工具循环熔断: %s x%d", tool_name, state.consecutive_same_tool_calls)
            return PermissionResultDeny(message=msg)

        # ── 反社工/反注入守卫 ──
        if tool_name in ("execute_command", "execute_python", "run_level2_cve_poc"):
            sabotage_text = str(
                tool_input.get("command", "")
                or tool_input.get("code", "")
                or tool_input.get("extra", "")
                or ""
            )
            # _looks_like_self_sabotage 当前返回 False，保留接口
            # is_sabotage, reason = _looks_like_self_sabotage(sabotage_text)

        if state.advisor_directive_pending and tool_name in ("execute_command", "execute_python", "run_level2_cve_poc"):
            latest_decision = str(
                (state.decision_history[-1] if state.decision_history else state.current_strategy)
                or ""
            ).strip()
            if not _advisor_decision_is_acknowledged(latest_decision):
                msg = (
                    "当前存在待执行顾问建议，已拦截本次工具调用。"
                    f"下一条助手回复必须先以 `{_ADVISOR_EXECUTION_PREFIX}` 开头，明确说明如何落实顾问建议，"
                    "然后才能调用工具。\n"
                    f"顾问建议: {state.advisor_directive_pending}"
                )
                _bounded_append(
                    state.action_history,
                    f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: 未先确认执行顾问建议",
                    limit=40,
                )
                _sync_progress_snapshot(state)
                return PermissionResultDeny(message=msg)

            command_text = str(tool_input.get("command", "") or "")
            if tool_name == "execute_command" and _looks_like_heavy_scan(command_text):
                msg = (
                    "已拦截偏离顾问建议的重扫描。"
                    "当前存在待执行顾问指令，下一步必须先落实顾问建议中的低成本验证，"
                    "不得继续跑 nmap/gobuster/ffuf/feroxbuster。\n"
                    f"顾问建议: {state.advisor_directive_pending}"
                )
                _bounded_append(
                    state.action_history,
                    f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: 待执行顾问建议时禁止重扫描",
                    limit=40,
                )
                _sync_progress_snapshot(state)
                return PermissionResultDeny(message=msg)

        # ── 鉴权快路径：禁止 shell 拼接认证头 ──
        if (
            not state.is_forum
            and not state.is_testenv
            and tool_name == "execute_command"
        ):
            command_text = str(tool_input.get("command", "") or "")
            if _looks_like_msf_command(command_text):
                msf_attempts = _recent_msf_attempt_count(state)
                if msf_attempts >= 1:
                    msg = (
                        "已拦截重复的 Metasploit/msf 调用。"
                        "Kali 里的 msf 链路不稳定，不要执着围绕 `msfconsole/msfvenom/msfrpcd` 反复重试；"
                        "请立即切换到本地 PoC、Impacket、Certipy、NetExec/CrackMapExec、Sliver 或原生命令链。"
                    )
                    _bounded_append(
                        state.action_history,
                        f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: 禁止重复 msf 调用",
                        limit=40,
                    )
                    state.current_strategy = msg
                    _sync_progress_snapshot(state)
                    return PermissionResultDeny(message=msg)
            combined = f"{state.recon_info}\n{state.memory_context}\n{' '.join(state.payload_history[-12:])}"
            if _looks_like_complex_http_bash(command_text):
                msg = (
                    "已拦截复杂 Bash HTTP 拼接。"
                    "当前请求包含 JSON/Header/Cookie/JWT 等复杂参数，"
                    "禁止继续用 `execute_command` + curl 硬拼；"
                    "直接改用 `execute_python(requests.Session())` 发包。"
                )
                state.current_strategy = msg
                _bounded_append(
                    state.action_history,
                    f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: 复杂 HTTP 请求必须改用 execute_python",
                    limit=40,
                )
                _sync_progress_snapshot(state)
                return PermissionResultDeny(message=msg)
            if _has_auth_surface(combined) and _looks_like_auth_shell(command_text):
                msg = (
                    "已拦截主战场鉴权后的 shell 认证请求。"
                    "当前题目已经出现 JWT/Bearer/Cookie 信号，"
                    "下一步禁止继续用 curl + shell 变量拼认证头；"
                    "直接改用 `execute_python(requests.Session())` 登录并访问受保护接口。"
                )
                _bounded_append(
                    state.action_history,
                    f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: 鉴权快路径禁止 shell 拼接认证头",
                    limit=40,
                )
                _sync_progress_snapshot(state)
                return PermissionResultDeny(message=msg)

            if _has_http_surface(combined) and _looks_like_heavy_scan(command_text):
                if _has_auth_surface(combined) or (state.attempts <= 2 and not _has_consumed_baseline(" ".join(state.payload_history[-12:]))):
                    msg = (
                        "已拦截低收益重扫描。"
                        "当前应先吃干净低成本 HTTP 信号，或直接沿 `/token` / `openapi.json` 快路径推进，"
                        "不要回头跑 `nmap/gobuster/ffuf`。"
                    )
                    _bounded_append(
                        state.action_history,
                        f"[#{state.attempts}] 拦截工具: {tool_name} | 原因: HTTP 基线未吃完前禁止重扫描",
                        limit=40,
                    )
                    _sync_progress_snapshot(state)
                    return PermissionResultDeny(message=msg)

        # ── Forum-2 Key 泄露守卫 ──
        if state.is_forum and int(state.challenge.get("forum_challenge_id", 0) or 0) == 2:
            if tool_name in _FORUM2_OUTBOUND_TOOLS:
                outbound = str(
                    tool_input.get("content", "")
                    or f"{tool_input.get('title', '')} {tool_input.get('content', '')}"
                    or ""
                ).strip()
                block_reason = _detect_forum2_key_disclosure(outbound)
                if block_reason:
                    msg = (
                        f"已拦截 forum-2 外发内容：{block_reason}。\n"
                        "红线：不得向任何其他 Agent 泄露我方 Key 的类型归属、完整值、前缀、局部片段。\n"
                        "改法：只索取对方的 Key 类型和前4位，只声明规则、施压或留证，不要回发我方任何 Key 信息。"
                    )
                    _bounded_append(
                        state.action_history,
                        f"[#{state.attempts}] 拦截外发: {tool_name} | {block_reason}",
                        limit=40,
                    )
                    _sync_progress_snapshot(state)
                    return PermissionResultDeny(message=msg)

        # 记录工具调用
        step = state.attempts + 1
        args_preview = str(tool_input)[:240]
        _bounded_append(state.action_history, f"[#{step}] 工具: {tool_name} | 参数: {args_preview}", limit=40)
        _bounded_append(state.payload_history, f"{tool_name} | {args_preview}", limit=20)
        state.attempts = step
        if state.advisor_directive_pending and tool_name in ("execute_command", "execute_python", "run_level2_cve_poc"):
            logger.info(
                "[Advisor] 已开始执行顾问建议: challenge=%s tool=%s directive=%s",
                state.challenge.get("code", "unknown"),
                tool_name,
                _clip_log_text(state.advisor_directive_pending, 220),
            )
            state.advisor_directive_pending = ""
        _sync_progress_snapshot(state)
        logger.info("[SDK] 工具调用: challenge=%s step=%s tool=%s payload=%s",
                    state.challenge.get("code", "unknown"),
                    state.attempts,
                    tool_name,
                    args_preview)

        return PermissionResultAllow()

    return can_use_tool


async def _call_advisor(
    state: RunnerState,
    advisor_model: Optional[str],
    latest_decision: str,
    latest_tool_result: str,
    *,
    reason: str = "",
) -> str:
    """调用顾问 subagent（替代 advisor_node）"""
    challenge = state.challenge
    challenge_code = challenge.get("code", "unknown")
    challenge_info = f"题目: {challenge.get('code', '')} | 目标: {', '.join(challenge.get('entrypoint') or [])}"
    prompt_template = FORUM_ADVISOR_PROMPT if state.is_forum else MAIN_BATTLE_ADVISOR_PROMPT
    state.advisor_call_count += 1
    advisor_call_id = state.advisor_call_count

    knowledge_context = ""
    state.knowledge_call_count += 1
    knowledge_call_id = state.knowledge_call_count
    try:
        from memory.knowledge_gateway import build_knowledge_advisor_context

        knowledge_context = build_knowledge_advisor_context(
            challenge,
            recon_info=state.recon_info,
            action_history=state.action_history,
            consecutive_failures=state.consecutive_failures,
        )
        knowledge_sources = _summarize_knowledge_sources(knowledge_context)
        knowledge_entry = (
            f"kb#{knowledge_call_id} reason={reason or '—'} sources={knowledge_sources} "
            f"hit={'yes' if knowledge_context.strip() else 'no'} "
            f"payload={_clip_log_text(knowledge_context or '—', 320)}"
        )
        _bounded_append(state.knowledge_history, knowledge_entry, limit=8)
        logger.info(
            "[Knowledge] 顾问知识库调用: challenge=%s call=%s reason=%s sources=%s hit=%s payload=%s",
            challenge_code,
            knowledge_call_id,
            reason or "—",
            knowledge_sources,
            bool(knowledge_context.strip()),
            _clip_log_text(knowledge_context or "—", 900),
        )
        _sync_progress_snapshot(state)
    except Exception as kb_exc:
        _bounded_append(
            state.knowledge_history,
            f"kb#{knowledge_call_id} reason={reason or '—'} error={_clip_log_text(kb_exc, 220)}",
            limit=8,
        )
        logger.warning(
            "[Knowledge] 顾问知识库调用失败: challenge=%s call=%s reason=%s err=%s",
            challenge_code,
            knowledge_call_id,
            reason or "—",
            kb_exc,
        )
        _sync_progress_snapshot(state)

    advisor_prompt_parts = [
        f"当前题目描述:\n{challenge_info}\n\n"
        f"最新一条主攻手决策:\n{latest_decision}\n\n"
        f"最近一条工具返回:\n{latest_tool_result}"
    ]
    if knowledge_context:
        advisor_prompt_parts.append(f"知识库参考:\n{knowledge_context}")
    advisor_prompt = "\n\n".join(part for part in advisor_prompt_parts if part)

    try:
        from config import load_config
        from llm.provider import create_llm_from_config
        from langchain_core.messages import HumanMessage, SystemMessage as LCSystemMessage

        cfg = load_config()
        advisor_llm = create_llm_from_config(cfg.llm, role="advisor")
        logger.info(
            "[Advisor] 调用开始: challenge=%s call=%s reason=%s model=%s prompt=%s",
            challenge_code,
            advisor_call_id,
            reason or "—",
            advisor_model or "provider_resolved",
            _clip_log_text(advisor_prompt, 1400),
        )
        async with asyncio.timeout(_ADVISOR_CALL_TIMEOUT_SECONDS):
            response = await advisor_llm.ainvoke(
                [
                    LCSystemMessage(content=prompt_template),
                    HumanMessage(content=advisor_prompt),
                ]
            )
        suggestion = _extract_langchain_text_content(getattr(response, "content", response))
    except TimeoutError:
        _bounded_append(
            state.advisor_history,
            f"advisor#{advisor_call_id} reason={reason or '—'} fallback=yes error=timeout_after_{_ADVISOR_CALL_TIMEOUT_SECONDS:.1f}s",
            limit=8,
        )
        _sync_progress_snapshot(state)
        logger.warning(
            "[Advisor] 调用超时: challenge=%s call=%s reason=%s timeout=%.1fs",
            challenge_code,
            advisor_call_id,
            reason or "—",
            _ADVISOR_CALL_TIMEOUT_SECONDS,
        )
        return _ADVISOR_FALLBACK_MESSAGE
    except Exception as exc:
        _bounded_append(
            state.advisor_history,
            f"advisor#{advisor_call_id} reason={reason or '—'} fallback=yes error={_clip_log_text(exc, 220)}",
            limit=8,
        )
        _sync_progress_snapshot(state)
        logger.warning(
            "[Advisor] 调用失败: challenge=%s call=%s reason=%s err=%s",
            challenge_code,
            advisor_call_id,
            reason or "—",
            exc,
        )
        return _ADVISOR_FALLBACK_MESSAGE

    if not suggestion:
        suggestion = "继续当前方向，但下一步必须做一次低成本高信号验证。"
    _bounded_append(
        state.advisor_history,
        f"advisor#{advisor_call_id} reason={reason or '—'} suggestion={_clip_log_text(suggestion, 280)}",
        limit=8,
    )
    _sync_progress_snapshot(state)
    logger.info(
        "[Advisor] 建议: challenge=%s call=%s reason=%s suggestion=%s",
        challenge_code,
        advisor_call_id,
        reason or "—",
        _clip_log_text(suggestion, 320),
    )
    return suggestion


async def run_agent(
    challenge: dict,
    system_prompt: str,
    initial_prompt: str,
    *,
    model: Optional[str] = None,
    advisor_model: Optional[str] = None,
    max_turns: int = 70,
    tool_loop_break_threshold: int = 20,
    advisor_no_tool_threshold: int = 2,
    advisor_consultation_interval: int = 0,
    consecutive_failures_threshold: int = 3,
    recon_info: str = "",
    zone_strategy: str = "",
    memory_context: str = "",
    skill_context: str = "",
    enabled_skills: list = None,
    progress_snapshot: dict[str, Any] | None = None,
    mcp_servers: dict = None,
    allowed_tools: list = None,
    cwd: Optional[str] = None,
    permission_mode: str = "bypassPermissions",
) -> dict:
    """
    核心 SDK runner（替代 build_graph() + graph.ainvoke()）。

    Returns:
        {is_finished, flag, flags_scored_count, expected_flag_count,
         scored_flags, attempts, action_history, payload_history,
         rejected_flags, consecutive_llm_errors, last_llm_error}
    """
    is_forum = bool(challenge.get("forum_task", False))
    is_testenv = bool(challenge.get("manual_task", False) and not is_forum)

    state = RunnerState(
        challenge=challenge,
        is_forum=is_forum,
        is_testenv=is_testenv,
        max_turns=max_turns,
        tool_loop_break_threshold=tool_loop_break_threshold,
        advisor_no_tool_threshold=advisor_no_tool_threshold,
        advisor_consultation_interval=advisor_consultation_interval,
        consecutive_failures_threshold=consecutive_failures_threshold,
        flags_scored_count=int(challenge.get("flag_got_count", 0) or 0),
        expected_flag_count=max(1, int(challenge.get("flag_count", 1) or 1)),
        recon_info=recon_info,
        zone_strategy=zone_strategy,
        memory_context=memory_context,
        skill_context=skill_context,
        enabled_skills=list(enabled_skills or []),
        progress_snapshot=progress_snapshot,
    )

    # 动态系统提示
    base_system_prompt = system_prompt or _build_system_prompt(state)
    challenge_code = challenge.get("code", "unknown")
    state.system_prompt_excerpt = _clip_log_text(base_system_prompt, 1800)
    state.initial_prompt_excerpt = _clip_log_text(initial_prompt, 1600)
    state.memory_context_excerpt = _clip_log_text(memory_context, 1200)
    state.skill_context_excerpt = _clip_log_text(skill_context, 1200)
    logger.info(
        "[SDK] Agent启动: challenge=%s model=%s advisor_model=%s max_turns=%s permission=%s forum=%s testenv=%s",
        challenge_code,
        model or "default",
        advisor_model or "default",
        max_turns,
        permission_mode,
        is_forum,
        is_testenv,
    )
    logger.info(
        "[SDK] 主攻提示词(system): challenge=%s payload=%s",
        challenge_code,
        state.system_prompt_excerpt or "—",
    )
    logger.info(
        "[SDK] 主攻提示词(user): challenge=%s payload=%s",
        challenge_code,
        state.initial_prompt_excerpt or "—",
    )
    logger.info(
        "[SDK] 上下文注入: challenge=%s enabled_skills=%s memory_payload=%s skill_payload=%s",
        challenge_code,
        ",".join(state.enabled_skills) if state.enabled_skills else "—",
        state.memory_context_excerpt or "—",
        state.skill_context_excerpt or "—",
    )
    _sync_progress_snapshot(state)

    response_turn_budget = _resolve_response_turn_budget(
        max_turns=max_turns,
        advisor_no_tool_threshold=advisor_no_tool_threshold,
        consecutive_failures_threshold=consecutive_failures_threshold,
        advisor_consultation_interval=advisor_consultation_interval,
    )
    options = ClaudeCodeOptions(
        system_prompt=base_system_prompt,
        max_turns=response_turn_budget,
        permission_mode=permission_mode,
        model=model,
        env=_build_main_sdk_env(),
        can_use_tool=_make_can_use_tool(state),
        mcp_servers=mcp_servers or {},
        allowed_tools=allowed_tools or [],
        cwd=cwd,
    )

    last_assistant_text = ""
    last_tool_result = ""
    advisor_injected = False
    meaningful_response_seen = False
    last_periodic_advisor_turn = 0
    total_turns_used = 0

    logger.info(
        "[SDK] 响应分段预算: challenge=%s global_max_turns=%s response_turn_budget=%s advisor_interval=%s no_tool_threshold=%s failure_threshold=%s",
        challenge_code,
        max_turns,
        response_turn_budget,
        advisor_consultation_interval,
        advisor_no_tool_threshold,
        consecutive_failures_threshold,
    )

    async def _process_response(client) -> bool:
        """处理一轮响应，返回 True 表示需要继续循环。"""
        nonlocal last_assistant_text, last_tool_result, advisor_injected, meaningful_response_seen, last_periodic_advisor_turn, total_turns_used
        first_message_logged = False
        async for msg in _iterate_with_timeouts(
            client.receive_response(),
            first_timeout=_SDK_FIRST_RESPONSE_TIMEOUT_SECONDS,
            idle_timeout=_SDK_IDLE_RESPONSE_TIMEOUT_SECONDS,
            counts_as_progress=_message_counts_as_progress,
        ):
            if not first_message_logged:
                first_message_logged = True
                logger.info(
                    "[SDK] 首包收到: challenge=%s type=%s",
                    challenge_code,
                    type(msg).__name__,
                )
            if isinstance(msg, SystemMessage):
                failure_reason = _system_message_failure_reason(msg)
                logger.info(
                    "[SDK] 系统消息: challenge=%s subtype=%s payload=%s",
                    challenge_code,
                    getattr(msg, "subtype", ""),
                    _clip_log_text(getattr(msg, "data", {}), 320),
                )
                if failure_reason:
                    raise RuntimeError(failure_reason)
                continue
            if isinstance(msg, UserMessage):
                meaningful_response_seen = True
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        tool_name = str(getattr(block, "name", "") or state.last_tool_name or "unknown")
                        is_error = bool(getattr(block, "is_error", False))
                        result_text = block.content if isinstance(block.content, str) else str(block.content or "")
                        result_excerpt = _clip_log_text(result_text, 700)
                        last_tool_result = f"{tool_name}: {result_excerpt}" if result_excerpt else tool_name
                        logger.info(
                            "[SDK] 工具结果: challenge=%s tool=%s status=%s payload=%s",
                            challenge_code,
                            tool_name,
                            "error" if is_error else "ok",
                            result_excerpt or "—",
                        )
                        if is_error:
                            state.consecutive_failures += 1
                            _bounded_append(
                                state.action_history,
                                f"[#{state.attempts}] 工具失败: {tool_name} | 返回: {_clip_log_text(result_excerpt, 220)}",
                                limit=40,
                            )
                        else:
                            state.consecutive_failures = 0
                            _bounded_append(
                                state.action_history,
                                f"[#{state.attempts}] 工具返回: {tool_name} | 摘要: {_clip_log_text(result_excerpt, 220)}",
                                limit=40,
                            )
                            if "🎉" in result_text and not state.is_forum:
                                progress = apply_main_battle_score_progress(
                                    content=result_text,
                                    submitted_flag=None,
                                    current_flag=state.flag,
                                    scored_flags=state.scored_flags,
                                    flags_scored_count=state.flags_scored_count,
                                    expected_flag_count=state.expected_flag_count,
                                )
                                state.flags_scored_count = progress["flags_scored_count"]
                                state.expected_flag_count = progress["expected_flag_count"]
                                state.scored_flags = progress["scored_flags"]
                                state.flag = progress["flag"]
                                state.is_finished = bool(progress["is_finished"])
                                if progress.get("continue_message"):
                                    logger.info("[SDK] %s", progress["continue_message"])
                                else:
                                    logger.info("[SDK] Flag 提交成功，题目完成")
                        _sync_progress_snapshot(state)
            elif isinstance(msg, AssistantMessage):
                meaningful_response_seen = True
                text_parts = []
                has_tool = False
                for block in msg.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        has_tool = True
                if text_parts:
                    last_assistant_text = _clip_log_text(" ".join(text_parts), 800)
                    state.current_strategy = last_assistant_text
                    _bounded_append(state.decision_history, last_assistant_text, limit=12)
                    logger.info(
                        "[SDK] 主攻思路: challenge=%s decision=%s payload=%s",
                        challenge_code,
                        len(state.decision_history),
                        last_assistant_text,
                    )
                    _sync_progress_snapshot(state)
                if not has_tool:
                    state.no_tool_rounds += 1
                else:
                    state.no_tool_rounds = 0
                    advisor_injected = False
                _sync_progress_snapshot(state)
            elif isinstance(msg, ResultMessage):
                meaningful_response_seen = True
                response_turns = max(0, int(getattr(msg, "num_turns", 0) or 0))
                total_turns_used += response_turns
                logger.info(
                    "[SDK] 轮次结束: challenge=%s response_turns=%s total_turns=%s cost=%s no_tool_rounds=%s consecutive_failures=%s finished=%s",
                    challenge_code,
                    response_turns,
                    total_turns_used,
                    msg.total_cost_usd,
                    state.no_tool_rounds,
                    state.consecutive_failures,
                    state.is_finished,
                )
                if state.is_finished or total_turns_used >= max_turns or state.attempts >= max_turns:
                    return False
                advisor_reasons = _build_advisor_reasons(
                    no_tool_rounds=state.no_tool_rounds,
                    advisor_no_tool_threshold=advisor_no_tool_threshold,
                    consecutive_failures=state.consecutive_failures,
                    consecutive_failures_threshold=consecutive_failures_threshold,
                    advisor_consultation_interval=advisor_consultation_interval,
                    total_turns=total_turns_used,
                    last_periodic_advisor_turn=last_periodic_advisor_turn,
                )
                should_advise = (
                    not advisor_injected and bool(advisor_reasons)
                )
                if should_advise:
                    advisor_reason = ", ".join(advisor_reasons)
                    advisor_injected = True
                    if any(reason.startswith("periodic_consultation(") for reason in advisor_reasons):
                        last_periodic_advisor_turn = _next_periodic_advisor_turn(
                            advisor_consultation_interval=advisor_consultation_interval,
                            last_periodic_advisor_turn=last_periodic_advisor_turn,
                        )
                    logger.info(
                        "[Advisor] 触发: challenge=%s reason=%s latest_decision=%s latest_tool_result=%s",
                        challenge_code,
                        advisor_reason,
                        _clip_log_text(last_assistant_text or "—", 320),
                        _clip_log_text(last_tool_result or "—", 320),
                    )
                    suggestion = await _call_advisor(
                        state,
                        advisor_model,
                        last_assistant_text,
                        last_tool_result,
                        reason=advisor_reason,
                    )
                    if _should_enforce_advisor_directive(suggestion):
                        state.current_strategy = f"顾问强制约束: {suggestion}"
                        state.advisor_directive_pending = suggestion
                        state.consecutive_failures = 0
                        state.no_tool_rounds = 0
                        _sync_progress_snapshot(state)
                        followup_prompt = _build_advisor_followup_prompt(suggestion)
                        logger.info(
                            "[Advisor] 注入执行指令: challenge=%s reason=%s payload=%s",
                            challenge_code,
                            advisor_reason,
                            _clip_log_text(followup_prompt, 420),
                        )
                        await client.query(followup_prompt)
                    else:
                        state.current_strategy = "顾问本轮不可用，按当前证据继续推进。"
                        state.advisor_directive_pending = ""
                        _sync_progress_snapshot(state)
                        logger.info(
                            "[Advisor] 本轮跳过强制注入: challenge=%s reason=%s suggestion=%s",
                            challenge_code,
                            advisor_reason,
                            _clip_log_text(suggestion, 220),
                        )
                        await client.query("继续。顾问本轮不可用，根据当前证据推进下一步攻击，必须调用工具。")
                else:
                    # 没有顾问注入时，发送继续指令驱动下一轮
                    logger.info(
                        "[SDK] 自动续跑: challenge=%s no_tool_rounds=%s consecutive_failures=%s",
                        challenge_code,
                        state.no_tool_rounds,
                        state.consecutive_failures,
                    )
                    await client.query("继续。根据当前证据推进下一步攻击，必须调用工具。")
                return True
        return False

    session_semaphore, session_limit = _get_sdk_session_semaphore()
    last_error: Exception | None = None
    startup_attempts = max(1, _SDK_STARTUP_RETRY_ATTEMPTS)
    for startup_attempt in range(1, startup_attempts + 1):
        client = ClaudeSDKClient(options)
        client_started = False
        meaningful_response_seen = False
        retry_requested = False
        logger.info(
            "[SDK] 等待会话并发槽: challenge=%s limit=%s attempt=%s/%s",
            challenge_code,
            session_limit,
            startup_attempt,
            startup_attempts,
        )
        async with session_semaphore:
            logger.info(
                "[SDK] 获得会话并发槽: challenge=%s limit=%s attempt=%s/%s",
                challenge_code,
                session_limit,
                startup_attempt,
                startup_attempts,
            )
            try:
                logger.info("[SDK] 建连开始: challenge=%s", challenge_code)
                async with asyncio.timeout(_SDK_CONNECT_TIMEOUT_SECONDS):
                    await client.__aenter__()
                client_started = True
                _register_sdk_handle(client, label="client", challenge_code=challenge_code)
                logger.info("[SDK] 建连完成: challenge=%s", challenge_code)
                logger.info("[SDK] 首轮请求发送: challenge=%s", challenge_code)
                async with asyncio.timeout(_SDK_INITIAL_QUERY_TIMEOUT_SECONDS):
                    await client.query(initial_prompt)
                while await _process_response(client):
                    pass
                last_error = None
                break
            except Exception as e:
                last_error = e
                error_text = str(e)
                should_retry = (
                    startup_attempt < startup_attempts
                    and not meaningful_response_seen
                    and state.attempts == 0
                    and not state.is_finished
                    and _looks_like_sdk_startup_failure(error_text)
                )
                retry_requested = should_retry
                logger.error("[SDK] 异常: %s", e)
                if should_retry:
                    logger.warning(
                        "[SDK] 启动阶段失败，准备重试: challenge=%s attempt=%s/%s err=%s",
                        challenge_code,
                        startup_attempt,
                        startup_attempts,
                        error_text,
                    )
                else:
                    state.last_llm_error = error_text
                    state.consecutive_llm_errors += 1
                    _sync_progress_snapshot(state)
            finally:
                try:
                    if client_started:
                        await _await_sdk_close(
                            client.__aexit__(None, None, None),
                            label="client",
                            challenge_code=challenge_code,
                            timeout=_SDK_CLOSE_TIMEOUT_SECONDS,
                            ignore_error_markers=_SDK_CLOSE_SCOPE_MARKERS,
                        )
                finally:
                    _unregister_sdk_handle(client)
                _sync_progress_snapshot(state)
        if last_error is None or not retry_requested:
            break
        backoff_seconds = _sdk_startup_retry_backoff_seconds(startup_attempt)
        logger.warning(
            "[SDK] 启动重试退避: challenge=%s attempt=%s/%s backoff=%.1fs err=%s",
            challenge_code,
            startup_attempt,
            startup_attempts,
            backoff_seconds,
            _clip_log_text(last_error, 220),
        )
        await asyncio.sleep(backoff_seconds)

    return {
        "is_finished": state.is_finished,
        "flag": state.flag,
        "flags_scored_count": state.flags_scored_count,
        "expected_flag_count": state.expected_flag_count,
        "scored_flags": state.scored_flags[-8:],
        "attempts": state.attempts,
        "action_history": state.action_history[-20:],
        "payload_history": state.payload_history[-8:],
        "decision_history": state.decision_history[-12:],
        "advisor_call_count": state.advisor_call_count,
        "advisor_history": state.advisor_history[-8:],
        "knowledge_call_count": state.knowledge_call_count,
        "knowledge_history": state.knowledge_history[-8:],
        "system_prompt_excerpt": state.system_prompt_excerpt,
        "initial_prompt_excerpt": state.initial_prompt_excerpt,
        "memory_context_excerpt": state.memory_context_excerpt,
        "skill_context_excerpt": state.skill_context_excerpt,
        "rejected_flags": state.rejected_flags[-16:],
        "consecutive_llm_errors": state.consecutive_llm_errors,
        "last_llm_error": state.last_llm_error,
        "current_strategy": state.current_strategy,
    }


def build_mcp_servers(challenge: dict) -> dict:
    """
    构建 MCP servers 配置（替代 get_tools_for_challenge()）。
    返回 ClaudeCodeOptions.mcp_servers 所需的 dict。
    """
    import sys
    import os
    python = sys.executable
    server_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "mcp_server.py")

    is_forum = bool(challenge.get("forum_task", False))
    is_testenv = bool(challenge.get("manual_task", False) and not is_forum)
    code = challenge.get("code", "")
    forum_id = int(challenge.get("forum_challenge_id", 0) or 0)

    servers: dict = {}

    # 主工具 server（shell + python + platform/forum tools）
    args = [server_script]
    if code and not is_forum:
        args += ["--challenge-code", code]
    if is_forum and forum_id:
        args += ["--forum-id", str(forum_id)]
    servers["lingxi"] = {"command": python, "args": args}

    # Sliver MCP（内网 C2）— 默认附加到 Z3/Z4 / 显式 sliver_task 题目
    if _should_attach_sliver_mcp(challenge):
        import os
        from config import load_config
        try:
            cfg = load_config()
            client_path = os.path.abspath(cfg.sliver.client_path)
            config_path = os.path.abspath(cfg.sliver.client_config_path)
            if os.path.exists(client_path) and os.path.exists(config_path):
                servers["sliver"] = {
                    "command": client_path,
                    "args": ["mcp", "--config", config_path],
                }
        except Exception:
            pass

    # Kali MCP（内网扫描/认证/横向）— 对 Level3+ 题目直接暴露 stdio client
    if _should_attach_kali_mcp(challenge):
        from config import load_config
        try:
            try:
                from tools.kali_mcp import DEFAULT_KALI_SERVER_PORT, KALI_CLIENT_PATH
            except Exception:
                DEFAULT_KALI_SERVER_PORT = 5001
                KALI_CLIENT_PATH = "/usr/share/mcp-kali-server/client.py"
            cfg = load_config()
            container = str(getattr(cfg.docker, "container_name", "") or "").strip()
            server_port = int(
                os.getenv("KALI_MCP_SERVER_PORT", str(DEFAULT_KALI_SERVER_PORT))
                or DEFAULT_KALI_SERVER_PORT
            )
            if container:
                servers["kali"] = {
                    "command": "docker",
                    "args": [
                        "exec",
                        "-i",
                        container,
                        "python3",
                        KALI_CLIENT_PATH,
                        "--server",
                        f"http://127.0.0.1:{server_port}",
                    ],
                }
        except Exception:
            pass

    return servers
