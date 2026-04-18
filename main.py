"""
Ling-Xi — Autonomous Penetration Testing Intelligence
=====================================================
自主渗透测试 Agent 主入口。

用法:
  python main.py                    # 仅主战场
  python main.py --lj               # 仅运行灵境/零界论坛题
  python main.py --all              # 主战场 + 灵境/零界双开
  python main.py --web              # 启动 Web Dashboard
  python main.py --all --web        # 双开并启动 Dashboard
  python main.py --web --port 8080  # 指定 Dashboard 端口
"""
import argparse
import asyncio
import copy
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Dict
from level2_task_hints import resolve_level2_task_hint
from log_utils import resolve_log_file, setup_logging, safe_endpoint_label
from runtime_env import ensure_project_venv

if __name__ == "__main__":
    ensure_project_venv()

# ─── 日志 ───
setup_logging(resolve_log_file())
logger = logging.getLogger("lingxi")
_IDLE_FETCH_SECONDS = max(1, int(os.getenv("LING_XI_IDLE_FETCH_SECONDS", "3") or 3))
_BUSY_FETCH_SECONDS = max(
    _IDLE_FETCH_SECONDS,
    int(os.getenv("LING_XI_BUSY_FETCH_SECONDS", "30") or 30),
)
_TASK_SHUTDOWN_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("LINGXI_TASK_SHUTDOWN_TIMEOUT", "12") or 12),
)


def _scheduler_refresh_interval_seconds(*, is_idle: bool) -> int:
    return _IDLE_FETCH_SECONDS if is_idle else _BUSY_FETCH_SECONDS


def _should_keep_instance_running_after_success(zone_level: int, flag_got: int, flag_count: int) -> bool:
    return zone_level >= 3 and flag_got < flag_count


def _should_consume_scheduler_event(event: asyncio.Event) -> bool:
    if not event.is_set():
        return False
    event.clear()
    return True


def _is_forum_runtime_key(runtime_key: str) -> bool:
    return str(runtime_key or "").strip().startswith("forum-")


def _compute_main_dispatch_budget(
    *,
    main_task_limit: int,
    scheduler_active_tasks: Dict | None = None,
    manual_active_tasks: Dict | None = None,
    queued_codes: set[str] | None = None,
) -> int:
    active_count = len(scheduler_active_tasks or {}) + len(manual_active_tasks or {})
    queued_count = sum(
        1
        for runtime_key in (queued_codes or set())
        if not _is_forum_runtime_key(runtime_key)
    )
    return max(0, int(main_task_limit) - active_count - queued_count)


def _challenge_level_turn_budget(level: int, default_budget: int) -> int:
    overrides = {
        1: 300,
        2: 100,
        3: 750,
        4: 1200,
    }
    return overrides.get(int(level or 0), int(default_budget))


def _challenge_level_task_timeout(level: int, default_timeout: int, *, is_forum_task: bool = False) -> int:
    if is_forum_task:
        return int(default_timeout)
    if int(level or 0) == 4:
        return 7200
    return int(default_timeout)


def _is_platform_transition_conflict(error: str) -> bool:
    text = str(error or "")
    if not text:
        return False
    return (
        "正在启动中" in text
        or "正在停止中" in text
        or "已有实例正在启动或停止中" in text
    )


def _is_infra_failure(error: str) -> bool:
    text = str(error or "").lower()
    if not text:
        return False
    keywords = (
        "llm 调用失败",
        "llm 连续调用失败",
        "error code: 524",
        " 524",
        "http 502",
        "http 503",
        "http 504",
        "http 429",
        "rate limit",
        "timeout reading",
        "connection reset",
        "connection aborted",
        "connection refused",
        "network error",
        "api 连接失败",
        "平台 api",
        "论坛 mcp",
        "mcp 工具调用失败",
        "自动重连后仍失败",
        "temporarily unavailable",
        "server disconnected",
        "bad gateway",
        "gateway timeout",
        "api error: 503",
        "attempted to exit a cancel scope",
        "current cancel scope",
        "async_generator_athrow",
        "failed to parse jsonrpc message from server",
        "jsonrpc message",
    )
    return any(token in text for token in keywords)


def _clip_result_log_text(text: str | None, limit: int = 220) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _summarize_result_path(result: Dict) -> str:
    final_strategy = _clip_result_log_text(result.get("final_strategy", ""), 180)
    if final_strategy:
        return final_strategy
    thought_summary = _clip_result_log_text(result.get("thought_summary", ""), 180)
    if thought_summary:
        return thought_summary
    action_summary = _clip_result_log_text(result.get("action_summary", ""), 180)
    if action_summary:
        return action_summary
    actions = [str(item or "").strip() for item in list(result.get("action_history", []) or []) if str(item or "").strip()]
    if actions:
        tail = actions[-3:]
        return _clip_result_log_text(" -> ".join(tail), 220)
    error_text = _clip_result_log_text(result.get("error", ""), 180)
    if error_text:
        return f"error={error_text}"
    return "—"


def _build_timeout_result(
    *,
    started_at: float,
    progress_snapshot: Dict | None = None,
    initial_flag_got_count: int = 0,
    initial_flag_count: int = 1,
) -> Dict:
    snapshot = dict(progress_snapshot or {})
    flags_scored_count = int(snapshot.get("flags_scored_count", initial_flag_got_count) or 0)
    expected_flag_count = max(
        1,
        int(snapshot.get("expected_flag_count", initial_flag_count or 1) or 1),
    )
    challenge_completed = flags_scored_count >= expected_flag_count
    progress_made = flags_scored_count > int(initial_flag_got_count or 0)
    result: Dict = {
        "success": bool(progress_made or challenge_completed),
        "error": "timeout_after_partial_progress" if progress_made else "timeout",
        "attempts": int(snapshot.get("attempts", 0) or 0),
        "elapsed": round(max(0.0, time.time() - float(started_at)), 1),
    }
    if progress_made or challenge_completed or "flags_scored_count" in snapshot:
        result["flags_scored_count"] = flags_scored_count
        result["expected_flag_count"] = expected_flag_count
        result["challenge_completed"] = challenge_completed
        result["is_finished"] = challenge_completed
    for key in (
        "flag",
        "scored_flags",
        "action_history",
        "payloads",
        "decision_history",
        "advisor_call_count",
        "advisor_history",
        "knowledge_call_count",
        "knowledge_history",
        "system_prompt_excerpt",
        "initial_prompt_excerpt",
        "memory_context_excerpt",
        "skill_context_excerpt",
    ):
        value = snapshot.get(key)
        if value is not None:
            result[key] = value

    current_strategy = str(snapshot.get("current_strategy", "") or "").strip()
    if current_strategy:
        result["final_strategy"] = current_strategy
        result["thought_summary"] = _clip_result_log_text(current_strategy, 220)
    return result


def _collect_live_tasks(task_candidates: list[asyncio.Task | None]) -> list[asyncio.Task]:
    seen: set[int] = set()
    tasks: list[asyncio.Task] = []
    for task in task_candidates:
        if task is None or task.done():
            continue
        marker = id(task)
        if marker in seen:
            continue
        seen.add(marker)
        tasks.append(task)
    return tasks


async def _wait_for_task_shutdown(
    task_candidates: list[asyncio.Task | None],
    *,
    timeout: float = _TASK_SHUTDOWN_TIMEOUT_SECONDS,
) -> dict[str, int]:
    tasks = _collect_live_tasks(task_candidates)
    summary = {"awaited": len(tasks), "timed_out": 0}
    if not tasks:
        return summary
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        remaining = _collect_live_tasks(tasks)
        summary["timed_out"] = len(remaining)
        logger.warning(
            "[Main] 等待任务关闭超时: timeout=%.1fs pending=%s",
            timeout,
            summary["timed_out"],
        )
    return summary


def _emit_task_result_log(
    *,
    code: str,
    display_code: str,
    result: Dict,
    success: bool,
    cleanup_status: str = "",
) -> None:
    payloads = [str(item or "").strip() for item in list(result.get("payloads", []) or []) if str(item or "").strip()]
    actions = [str(item or "").strip() for item in list(result.get("action_history", []) or []) if str(item or "").strip()]
    scored_flags = [str(item or "").strip() for item in list(result.get("scored_flags", []) or []) if str(item or "").strip()]
    thought_summary = _clip_result_log_text(result.get("thought_summary", ""), 180)
    advisor_summary = _clip_result_log_text(result.get("advisor_summary", ""), 220)
    knowledge_summary = _clip_result_log_text(result.get("knowledge_summary", ""), 220)
    logger.info(
        "[TaskSummary] code=%s display=%s success=%s attempts=%s elapsed=%s flag=%s scored_flags=%s summary=%s thought=%s advisor_calls=%s knowledge_calls=%s cleanup=%s payloads=%s actions=%s",
        code,
        display_code,
        success,
        result.get("attempts", 0),
        result.get("elapsed", 0),
        result.get("flag", ""),
        scored_flags[-4:],
        _summarize_result_path(result),
        thought_summary or "—",
        result.get("advisor_call_count", 0),
        result.get("knowledge_call_count", 0),
        cleanup_status or "—",
        payloads[-6:],
        actions[-6:],
    )
    logger.info(
        "[TaskDetail] code=%s system_prompt=%s user_prompt=%s memory=%s skills=%s advisor=%s knowledge=%s",
        code,
        _clip_result_log_text(result.get("system_prompt_excerpt", ""), 320) or "—",
        _clip_result_log_text(result.get("initial_prompt_excerpt", ""), 320) or "—",
        _clip_result_log_text(result.get("memory_context_excerpt", ""), 320) or "—",
        _clip_result_log_text(result.get("skill_context_excerpt", ""), 320) or "—",
        advisor_summary or "—",
        knowledge_summary or "—",
    )


async def _probe_entrypoint_health(label: str, url: str) -> None:
    endpoint = safe_endpoint_label(url)
    if not url:
        logger.warning("[Probe] %s 未配置可探测入口", label)
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
        content_type = str(response.headers.get("content-type", "") or "").lower()
        if response.status_code == 404:
            logger.warning("[Probe] %s 可达但 API 返回 404: endpoint=%s", label, endpoint)
            return
        if "json" not in content_type:
            logger.warning(
                "[Probe] %s 可达但返回非 JSON: endpoint=%s status=%s content_type=%s",
                label,
                endpoint,
                response.status_code,
                content_type or "unknown",
            )
            return
        try:
            response.json()
        except ValueError:
            logger.warning(
                "[Probe] %s 可达但返回非法 JSON: endpoint=%s status=%s",
                label,
                endpoint,
                response.status_code,
            )
            return
        logger.info("[Probe] %s 健康探测完成: endpoint=%s status=%s", label, endpoint, response.status_code)
    except Exception as exc:
        logger.warning("[Probe] %s 健康探测失败: endpoint=%s err=%s", label, endpoint, exc)


async def _wait_for_instance_ready(entrypoint: list, timeout: int = 30) -> bool:
    """等待靶机端口可达，最多等 timeout 秒，返回是否就绪。"""
    import socket
    if not entrypoint:
        return True
    first = entrypoint[0]
    parts = first.rsplit(":", 1)
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 80
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
            writer.close()
            await writer.wait_closed()
            logger.info("[Probe] 靶机就绪: %s:%s", host, port)
            return True
        except Exception:
            await asyncio.sleep(3)
    logger.warning("[Probe] 靶机端口超时未就绪: %s:%s", host, port)
    return False


async def main(
    enable_web: bool = False,
    web_port: int = 7890,
    lj_only: bool = False,
    run_all: bool = False,
):
    from config import load_config, resolve_advisor_model_name
    from llm.provider import create_llm_from_config
    from tools.shell import (
        add_allowed_hosts,
        configure_command_guard,
        configure_shell,
        get_shell_runtime_state,
        scoped_command_policy,
    )
    from tools.platform_api import CompetitionAPIClient, set_api_client
    from tools.platform_api import run_platform_api_io
    from tools.forum_api import (
        ForumAPIClient,
        initialize_forum_mcp,
        reconnect_forum_services,
        shutdown_forum_mcp,
        set_forum_client,
    )
    from tools.sliver_mcp import (
        initialize_sliver_mcp,
        shutdown_sliver_mcp,
    )
    from tools.kali_mcp import (
        initialize_kali_mcp,
        shutdown_kali_mcp,
    )
    from tools.forum_history_bootstrap import run_forum_history_bootstrap
    from tools.forum_message_state import run_forum_message_state_worker
    from agent.sdk_solver import solve_challenge_sdk as solve_challenge
    from agent.sdk_runner import shutdown_active_sdk_sessions, _resolve_sdk_session_concurrency
    from agent.main_battle_progress import should_mark_challenge_solved
    from agent.scheduler import ZoneScheduler, ZONE_INFO, Zone
    from memory.store import get_memory_store
    from memory.knowledge_store import bucket_for_challenge
    from memory.knowledge_writeback import (
        enqueue_knowledge_writeback,
        knowledge_writeback_enabled,
        run_knowledge_writeback_worker,
    )
    from agent.console import (
        get_console, print_banner, print_config_table,
        print_zone_status, print_challenge_start, print_challenge_result,
        print_final_report,
    )

    console = get_console()
    loop = asyncio.get_running_loop()
    shutdown_future: asyncio.Future[str] = loop.create_future()

    def _request_shutdown(reason: str) -> None:
        if shutdown_future.done():
            return
        logger.info("[Main] 收到停止请求: %s", reason)
        shutdown_future.set_result(reason)

    installed_signal_handlers: list[int] = []
    for sig, reason in (
        (signal.SIGINT, "sigint"),
        (signal.SIGTERM, "sigterm"),
    ):
        try:
            loop.add_signal_handler(sig, _request_shutdown, reason)
            installed_signal_handlers.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    os.environ.setdefault("LING_XI_PYTHON", sys.executable)
    logger.info("[Runtime] Python executable=%s", sys.executable)
    forum_only_mode = bool(lj_only)
    dual_run_mode = bool(run_all)
    forum_bootstrap_task: asyncio.Task | None = None
    forum_message_state_task: asyncio.Task | None = None
    knowledge_writeback_task: asyncio.Task | None = None
    forum_bootstrap_delay_seconds = max(
        0.0,
        float(os.getenv("FORUM_HISTORY_BOOTSTRAP_DELAY_SECONDS", "2.0") or 2.0),
    )
    forum_bootstrap_refresh_seconds = max(
        30.0,
        float(os.getenv("FORUM_HISTORY_BOOTSTRAP_REFRESH_SECONDS", "300") or 300),
    )
    forum_bootstrap_cache_path = os.path.join("wp", "forum_history_bootstrap.json")

    async def _schedule_forum_history_bootstrap(*, reason: str, force: bool) -> bool:
        nonlocal forum_bootstrap_task
        if forum_bootstrap_task is not None and not forum_bootstrap_task.done():
            logger.info("[Forum] 历史私信扫描已在后台运行，跳过重复触发: reason=%s", reason)
            return False

        if not force and os.path.exists(forum_bootstrap_cache_path):
            try:
                cache_age = max(0.0, time.time() - os.path.getmtime(forum_bootstrap_cache_path))
            except OSError:
                cache_age = forum_bootstrap_refresh_seconds + 1.0
            if cache_age < forum_bootstrap_refresh_seconds:
                logger.info(
                    "[Forum] 历史私信摘要较新，跳过重复后台扫描: reason=%s age=%.1fs",
                    reason,
                    cache_age,
                )
                return False

        async def _runner() -> None:
            if forum_bootstrap_delay_seconds > 0:
                await asyncio.sleep(forum_bootstrap_delay_seconds)
            logger.info("[Forum] 历史私信后台扫描启动: reason=%s", reason)
            try:
                await asyncio.to_thread(run_forum_history_bootstrap, True)
                logger.info("[Forum] 历史私信后台扫描完成: reason=%s", reason)
            except Exception as bootstrap_exc:
                logger.warning("[Forum] 历史私信后台扫描失败: %s", bootstrap_exc)

        forum_bootstrap_task = asyncio.create_task(
            _runner(),
            name=f"forum-history-bootstrap:{reason}",
        )
        return True

    async def _ensure_forum_message_state_worker(*, reason: str) -> bool:
        nonlocal forum_message_state_task
        if forum_message_state_task is not None and not forum_message_state_task.done():
            return False
        forum_message_state_task = asyncio.create_task(
            run_forum_message_state_worker(submit_flags=False),
            name=f"forum-message-state:{reason}",
        )
        logger.info("[Forum] 私信状态机已转后台，不阻塞论坛任务: reason=%s", reason)
        return True

    # ─── Banner + 配置 ───
    print_banner()
    config = load_config()

    # 强制覆盖论坛配置：--forum-only 模式必须启用论坛
    if forum_only_mode:
        config.forum.enabled = True
        logger.info("[Runtime] --forum-only 模式强制启用论坛")

    default_executor_workers = max(
        16,
        int(config.agent.max_concurrent_tasks) * 8 + max(2, int(config.agent.max_forum_concurrent_tasks or 0) * 2),
    )
    loop.set_default_executor(ThreadPoolExecutor(max_workers=default_executor_workers))
    logger.info("[Runtime] 默认线程池已扩容: max_workers=%s", default_executor_workers)
    print_config_table(config)
    if not forum_only_mode and config.platform.api_base_url:
        await _probe_entrypoint_health(
            "主赛场 API",
            f"{config.platform.api_base_url.rstrip('/')}/api/challenges",
        )
    if config.forum.server_host:
        await _probe_entrypoint_health(
            "零界论坛 API",
            f"{config.forum.server_host.rstrip('/')}/api/v1/agent/flags/challenges",
        )
    if forum_only_mode:
        console.print("[lingxi.info]🧭 启动模式: --lj（仅灵境/零界论坛题）[/lingxi.info]")
        console.print()
    elif dual_run_mode:
        console.print("[lingxi.info]🧭 启动模式: --all（主战场 + 灵境/零界双开）[/lingxi.info]")
        console.print()

    # ─── 执行环境 ───
    configure_shell(config.docker.container_name, config.docker.enabled)
    shell_runtime = get_shell_runtime_state()

    # 增强的 Docker 初始化验证和重试逻辑
    if config.docker.enabled and shell_runtime.get("mode") != "docker":
        logger.warning(
            "[Runtime] Docker 执行环境未就绪，1 秒后重试: container=%s reason=%s",
            config.docker.container_name,
            shell_runtime.get("reason"),
        )
        time.sleep(1)
        configure_shell(config.docker.container_name, config.docker.enabled)
        shell_runtime = get_shell_runtime_state()

        # 第二次验证
        if shell_runtime.get("mode") != "docker":
            logger.warning(
                "[Runtime] Docker 初始化失败，尝试最后一次重新配置: container=%s",
                config.docker.container_name,
            )
            time.sleep(2)
            configure_shell(config.docker.container_name, config.docker.enabled)
            shell_runtime = get_shell_runtime_state()

    # 最终验证和降级处理
    if config.docker.enabled and shell_runtime.get("mode") != "docker":
        logger.error(
            "[Runtime] Docker 容器不可用，已降级为本地执行: container=%s reason=%s",
            config.docker.container_name,
            shell_runtime.get("reason"),
        )
        console.print(
            "[lingxi.warning]⚠️ Docker 容器未接管，当前将使用本地执行环境（sqlmap/nikto/gobuster 等能力可能受限）[/lingxi.warning]"
        )
    else:
        logger.info(
            "[Runtime] Shell 执行环境已确认: mode=%s container=%s reason=%s",
            shell_runtime.get("mode"),
            shell_runtime.get("container"),
            shell_runtime.get("reason"),
        )
    configure_command_guard(
        blocked_hosts=[
            "infra-gateway.example",
            config.platform.base_url,
            getattr(config.platform, "api_base_url", ""),
            config.forum.server_host,
            getattr(config.forum, "server_host_fallback", ""),
            config.llm.forum_llm_base_url,
            os.getenv("OPENAI_BASE_URL", ""),
            os.getenv("ANTHROPIC_BASE_URL", ""),
            os.getenv("ADVISOR_ANTHROPIC_BASE_URL", ""),
            os.getenv("DEEPSEEK_BASE_URL", ""),
            os.getenv("SILICONFLOW_BASE_URL", ""),
        ]
    )

    # ─── LLM ───
    try:
        main_llm = create_llm_from_config(config.llm, role="main")
        advisor_llm = create_llm_from_config(config.llm, role="advisor")
        forum_main_llm = advisor_llm
        forum_advisor_llm = advisor_llm
        forum_llm_label = "inherit-global-advisor"
        if config.forum.enabled:
            forum_llm_config = copy.deepcopy(config.llm)
            if config.llm.forum_llm_base_url and config.llm.forum_llm_api_key and config.llm.forum_llm_model:
                forum_llm_config.main_provider = config.llm.forum_llm_provider
                forum_llm_config.advisor_provider = config.llm.forum_llm_provider
                forum_llm_config.openai_base_url = config.llm.forum_llm_base_url
                forum_llm_config.openai_api_key = config.llm.forum_llm_api_key
                forum_llm_config.openai_model = config.llm.forum_llm_model
                try:
                    forum_main_llm = create_llm_from_config(forum_llm_config, role="main")
                    forum_advisor_llm = create_llm_from_config(forum_llm_config, role="advisor")
                    forum_llm_label = f"{config.llm.forum_llm_provider} / {config.llm.forum_llm_model}"
                except Exception as forum_llm_exc:
                    logger.warning(
                        "[LLM] 零界专用 LLM 初始化失败，回退到全局顾问模型: %s",
                        forum_llm_exc,
                    )
                    forum_main_llm = advisor_llm
                    forum_advisor_llm = advisor_llm
                    forum_llm_label = "fallback-global-advisor"
            else:
                forum_llm_config.main_provider = "anthropic"
                try:
                    forum_main_llm = create_llm_from_config(forum_llm_config, role="main")
                    forum_advisor_llm = advisor_llm
                    forum_llm_label = f"anthropic / {config.llm.anthropic_model}"
                except Exception as forum_llm_exc:
                    logger.warning(
                        "[LLM] 零界主攻模型覆盖初始化失败，回退到顾问模型: %s",
                        forum_llm_exc,
                    )
                    forum_main_llm = advisor_llm
                    forum_advisor_llm = advisor_llm
                    forum_llm_label = "fallback-global-advisor"
        console.print("[lingxi.success]✅ LLM 初始化完成[/lingxi.success]")
        if config.forum.enabled:
            console.print(
                f"[lingxi.success]✅ 零界 Agent LLM: {forum_llm_label}[/lingxi.success]"
            )
    except Exception as e:
        console.print(f"[lingxi.error]❌ LLM 初始化失败: {e}[/lingxi.error]")
        sys.exit(1)

    # ─── 比赛平台 API ───
    api_client = None
    platform_transport_available = False
    if forum_only_mode:
        console.print("[lingxi.info]ℹ️ --lj 模式跳过主战场平台 API 初始化[/lingxi.info]")
    else:
        try:
            api_client = CompetitionAPIClient(config.platform.api_base_url, config.platform.api_token)
            set_api_client(api_client)
            platform_transport_available = True
            console.print("[lingxi.success]✅ 平台 API 已连接[/lingxi.success]")
        except Exception as e:
            platform_transport_available = False
            console.print(f"[lingxi.warning]⚠️ 平台 API 初始化失败，进入自动重连模式: {e}[/lingxi.warning]")

    forum_client = None
    forum_transport_available = False
    sliver_transport_available = False
    if config.forum.enabled and config.forum.server_host and config.forum.agent_bearer_token:
        try:
            forum_client = ForumAPIClient(
                config.forum.server_host,
                config.forum.agent_bearer_token,
                config.forum.server_host_fallback,
            )
            set_forum_client(forum_client)
            initialize_forum_mcp(
                config.forum.server_host,
                config.forum.agent_bearer_token,
                config.forum.server_host_fallback,
            )
            forum_transport_available = True
            console.print("[lingxi.success]✅ 零界论坛 API 已连接[/lingxi.success]")
            console.print("[lingxi.success]✅ 零界论坛 MCP 已连接 (本地论坛扩展)[/lingxi.success]")
            if await _schedule_forum_history_bootstrap(reason="startup", force=True):
                logger.info("[Forum] 历史私信扫描已转后台，不阻塞启动")
            if forum_only_mode or dual_run_mode:
                await _ensure_forum_message_state_worker(reason="startup")
        except Exception as e:
            console.print(f"[lingxi.warning]⚠️ 零界论坛 API/MCP 初始化失败: {e}[/lingxi.warning]")
            forum_client = None
            forum_transport_available = False
    else:
        console.print("[lingxi.warning]⚠️ 零界论坛未启用或缺少配置[/lingxi.warning]")

    if config.sliver.enabled:
        try:
            initialize_sliver_mcp(
                config.sliver.client_path,
                config.sliver.client_config_path,
                config.sliver.client_root_dir,
            )
            sliver_transport_available = True
            console.print("[lingxi.success]✅ Sliver MCP 已连接 (sliver-client mcp)[/lingxi.success]")
        except Exception as e:
            sliver_transport_available = False
            console.print(f"[lingxi.warning]⚠️ Sliver MCP 初始化失败: {e}[/lingxi.warning]")
    else:
        console.print("[lingxi.info]ℹ️ Sliver MCP 未启用[/lingxi.info]")

    # ─── Kali MCP ───
    try:
        initialize_kali_mcp(container=config.docker.container_name)
        console.print("[lingxi.success]✅ Kali MCP 已连接 (kali-server-mcp)[/lingxi.success]")
    except Exception as e:
        console.print(f"[lingxi.warning]⚠️ Kali MCP 初始化失败: {e}[/lingxi.warning]")
    scheduler = ZoneScheduler(api_client, config) if api_client is not None else None
    memory = get_memory_store()
    if knowledge_writeback_enabled():
        knowledge_writeback_task = asyncio.create_task(
            run_knowledge_writeback_worker(memory_store=memory)
        )
    if scheduler is not None:
        console.print("[lingxi.success]✅ 赛区调度器就绪[/lingxi.success]")
    else:
        console.print("[lingxi.success]✅ 论坛任务调度就绪[/lingxi.success]")
    console.print("[lingxi.success]✅ 记忆系统就绪[/lingxi.success]")
    if knowledge_writeback_task is not None:
        console.print("[lingxi.success]✅ 结构化知识写回已启用[/lingxi.success]")
    console.print()

    # ─── Web Dashboard (可选) ───
    update_agent_state = None
    update_zones = None
    push_log = None
    upsert_task = None
    TaskRecord = None
    TaskStatus = None
    register_callbacks = None
    get_task_record = None

    if enable_web:
        from web.server import (
            start_web_server, update_agent_state, update_zones,
            push_log, upsert_task, TaskRecord, TaskStatus,
            register_callbacks, get_task_record,
        )
        await start_web_server(port=web_port)
        console.print(f"[lingxi.success]✅ Web Dashboard: http://localhost:{web_port}[/lingxi.success]")
        console.print()

    # ─── 并发控制 ───
    configured_main_task_limit = max(1, int(config.agent.max_concurrent_tasks))
    sdk_main_task_limit = max(1, _resolve_sdk_session_concurrency())
    main_task_limit = min(configured_main_task_limit, sdk_main_task_limit)
    if main_task_limit < configured_main_task_limit:
        logger.info(
            "[Runtime] 主战场并发已收敛: configured=%s sdk_limit=%s effective=%s",
            configured_main_task_limit,
            sdk_main_task_limit,
            main_task_limit,
        )
    forum_task_limit_cfg = int(config.agent.max_forum_concurrent_tasks or 0)
    forum_task_limit = forum_task_limit_cfg if forum_task_limit_cfg > 0 else 4
    main_semaphore = asyncio.Semaphore(main_task_limit)
    forum_semaphore = asyncio.Semaphore(max(1, forum_task_limit))
    scheduler_tick = max(1, config.agent.schedule_tick_seconds)
    start_time = time.time()
    scheduler_event = asyncio.Event()
    queued_codes = set()
    aborted_codes = set()
    spawn_lock = asyncio.Lock()
    solved_forum_codes = set()
    forum_failed_counts: Dict[str, int] = {}
    forum_cooldown_until: Dict[str, float] = {}
    last_zone_table_signature = None
    forum_active_tasks: Dict[str, asyncio.Task] = {}
    manual_active_tasks: Dict[str, asyncio.Task] = {}
    forum_transport_failure_streak = 0
    platform_transport_failure_streak = 0
    platform_instance_transition_lock = asyncio.Lock()
    platform_instance_transition_until = 0.0

    def _platform_transition_cooldown_seconds(*, busy: bool = False) -> float:
        base = max(1.0, min(10.0, float(config.agent.schedule_tick_seconds)))
        return max(base, 5.0) if busy else base

    async def _wait_for_platform_instance_transition_window() -> None:
        nonlocal platform_instance_transition_until
        remaining = platform_instance_transition_until - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _set_platform_instance_transition_window(cooldown_seconds: float) -> None:
        nonlocal platform_instance_transition_until
        next_ts = time.time() + max(0.0, cooldown_seconds)
        platform_instance_transition_until = max(platform_instance_transition_until, next_ts)

    async def _run_platform_instance_action(
        action: str,
        code: str,
        operation,
    ):
        async with platform_instance_transition_lock:
            await _wait_for_platform_instance_transition_window()
            try:
                result = await run_platform_api_io(operation, code)
                _set_platform_instance_transition_window(
                    _platform_transition_cooldown_seconds(busy=False)
                )
                return result
            except Exception as exc:
                message = str(exc)
                if _is_platform_transition_conflict(message):
                    _set_platform_instance_transition_window(
                        _platform_transition_cooldown_seconds(busy=True)
                    )
                raise

    async def _cleanup_main_instance_after_failure(code: str):
        if scheduler is None or api_client is None:
            return ""
        restart_cooldown_seconds = max(180, int(config.agent.retry_backoff_seconds) * 2)
        try:
            await _run_platform_instance_action("stop_after_failure", code, api_client.stop_challenge)
            scheduler.mark_instance_stopped(code)
            scheduler.mark_recently_stopped_unsolved(
                code,
                cooldown_seconds=restart_cooldown_seconds,
            )
            logger.info(
                "[Task] 失败后已停止实例: %s | restart_cooldown=%ss",
                code,
                restart_cooldown_seconds,
            )
            return f"✅ 失败后实例已停止（重启冷却 {restart_cooldown_seconds}s）"
        except Exception as stop_err:
            stop_error_text = str(stop_err)
            if "赛题实例未运行" in stop_error_text:
                scheduler.mark_instance_stopped(code)
                scheduler.mark_recently_stopped_unsolved(
                    code,
                    cooldown_seconds=restart_cooldown_seconds,
                )
            elif _is_platform_transition_conflict(stop_error_text):
                scheduler.mark_transient_failure(
                    code,
                    cooldown_seconds=max(
                        restart_cooldown_seconds,
                        int(_platform_transition_cooldown_seconds(busy=True)),
                    ),
                )
            logger.warning("[Task] 失败后停止实例失败: %s | %s", code, stop_err)
            return f"⚠️ 失败后实例停止失败: {stop_err}"

    async def _reclaim_completed_main_instances() -> None:
        if scheduler is None or api_client is None:
            return
        reclaimable = scheduler.get_reclaimable_running_instances()
        if not reclaimable:
            return
        for challenge in reclaimable:
            code = str(challenge.get("code", "") or "").strip()
            if not code:
                continue
            try:
                await _run_platform_instance_action("reclaim_completed", code, api_client.stop_challenge)
                scheduler.mark_instance_stopped(code)
                logger.info(
                    "[Scheduler] 回收已完成但残留运行中的实例: %s",
                    code,
                )
            except Exception as stop_err:
                if "赛题实例未运行" in str(stop_err):
                    scheduler.mark_instance_stopped(code)
                else:
                    scheduler.mark_transient_failure(
                        code,
                        cooldown_seconds=max(1, config.agent.retry_backoff_seconds),
                    )
                logger.warning(
                    "[Scheduler] 回收已完成实例失败: %s | %s",
                    code,
                    stop_err,
                )

    def _mark_runtime_failure(
        *,
        code: str,
        is_forum_task: bool,
        manual_task: bool,
        error: str,
    ) -> None:
        infra_failure = _is_infra_failure(error)
        backoff_seconds = max(1, config.agent.retry_backoff_seconds)
        if is_forum_task:
            if not infra_failure:
                forum_failed_counts[code] = forum_failed_counts.get(code, 0) + 1
            forum_cooldown_until[code] = time.time() + backoff_seconds
            return
        if manual_task or scheduler is None:
            return
        if infra_failure:
            scheduler.mark_transient_failure(code, cooldown_seconds=backoff_seconds)
        else:
            scheduler.mark_failed(code)

    def _active_task_map_for_challenge(challenge: Dict | None = None) -> Dict[str, asyncio.Task]:
        challenge = challenge or {}
        if challenge.get("forum_task"):
            return forum_active_tasks
        if challenge.get("manual_task"):
            return manual_active_tasks
        return scheduler.active_tasks if scheduler is not None else manual_active_tasks

    def _all_active_task_maps() -> tuple[Dict[str, asyncio.Task], ...]:
        maps: list[Dict[str, asyncio.Task]] = []
        if scheduler is not None:
            maps.append(scheduler.active_tasks)
        maps.extend([forum_active_tasks, manual_active_tasks])
        return tuple(maps)

    async def _ensure_forum_transport() -> bool:
        nonlocal forum_client, forum_transport_available, forum_transport_failure_streak
        if not config.forum.enabled or not config.forum.server_host or not config.forum.agent_bearer_token:
            forum_transport_available = False
            return False
        if forum_transport_available and forum_client is not None:
            return True
        try:
            forum_client = ForumAPIClient(
                config.forum.server_host,
                config.forum.agent_bearer_token,
                config.forum.server_host_fallback,
            )
            set_forum_client(forum_client)
            await asyncio.to_thread(
                reconnect_forum_services,
                config.forum.server_host,
                config.forum.agent_bearer_token,
                config.forum.server_host_fallback,
            )
            forum_transport_available = True
            forum_transport_failure_streak = 0
            await _schedule_forum_history_bootstrap(reason="reconnect", force=False)
            if forum_only_mode or dual_run_mode:
                await _ensure_forum_message_state_worker(reason="reconnect")
            logger.info("[Forum] API/MCP 自动重连成功")
            return True
        except Exception as exc:
            forum_transport_failure_streak += 1
            forum_transport_available = False
            logger.warning(
                "[Forum] API/MCP 自动重连失败 (%s): %s",
                forum_transport_failure_streak,
                exc,
            )
            return False

    async def _ensure_platform_transport() -> bool:
        nonlocal api_client, platform_transport_available, platform_transport_failure_streak
        if forum_only_mode or not config.platform.api_base_url:
            platform_transport_available = False
            return False
        if platform_transport_available and api_client is not None:
            return True
        try:
            api_client = CompetitionAPIClient(config.platform.api_base_url, config.platform.api_token)
            set_api_client(api_client)
            platform_transport_available = True
            platform_transport_failure_streak = 0
            logger.info("[Platform] API 自动重连成功")
            return True
        except Exception as exc:
            platform_transport_failure_streak += 1
            platform_transport_available = False
            logger.warning(
                "[Platform] API 自动重连失败 (%s): %s",
                platform_transport_failure_streak,
                exc,
            )
            return False

    def _build_forum_challenge_objects() -> list[Dict]:
        if forum_client is None or not forum_transport_available:
            return []
        try:
            forum_data = forum_client.get_challenges() or []
        except Exception as exc:
            logger.warning("[Forum] 拉取论坛题目失败: %s", exc)
            return []

        result = []
        difficulty_map = {1: "easy", 2: "medium", 3: "hard"}
        for item in forum_data:
            if not isinstance(item, dict):
                continue
            if not item.get("is_active", True):
                continue
            challenge_id = item.get("id")
            if challenge_id is None:
                continue
            code = f"forum-{challenge_id}"
            if code in solved_forum_codes:
                continue
            cooldown_until = forum_cooldown_until.get(code, 0.0)
            if cooldown_until > time.time():
                continue
            title = str(item.get("title", f"论坛赛题{challenge_id}") or f"论坛赛题{challenge_id}")
            description = str(item.get("description", "") or "")
            result.append(
                {
                    "code": code,
                    "display_code": title,
                    "runtime_key": code,
                    "memory_scope_key": code,
                    "task_id": None,
                    "title": title,
                    "description": description,
                    "difficulty": difficulty_map.get(int(item.get("difficulty", 2) or 2), "medium"),
                    "total_score": int(item.get("max_score", 0) or 0),
                    "entrypoint": [config.forum.server_host.rstrip("/")],
                    "instance_status": "running",
                    "level": 1,
                    "flag_count": int(item.get("flag_count", 1 if challenge_id in (1, 2, 4) else 0) or 0),
                    "flag_got_count": int(item.get("flag_got_count", 1 if code in solved_forum_codes else 0) or 0),
                    "manual_task": True,
                    "forum_task": True,
                    "forum_challenge_id": challenge_id,
                    "zone": "z1",
                }
            )
        return result

    def _task_runtime_key(task_id: str) -> str:
        return f"manual::{task_id}"

    def _challenge_runtime_key(challenge: Dict | None) -> str:
        challenge = challenge or {}
        runtime_key = str(challenge.get("runtime_key", "") or "").strip()
        if runtime_key:
            return runtime_key
        return str(challenge.get("code", "") or "").strip()

    def _zone_from_web_value(zone_value: str):
        mapping = {
            "z1": Zone.Z1_SRC,
            "z2": Zone.Z2_CVE,
            "z3": Zone.Z3_NETWORK,
            "z4": Zone.Z4_AD,
            Zone.Z1_SRC.value: Zone.Z1_SRC,
            Zone.Z2_CVE.value: Zone.Z2_CVE,
            Zone.Z3_NETWORK.value: Zone.Z3_NETWORK,
            Zone.Z4_AD.value: Zone.Z4_AD,
        }
        return mapping.get(zone_value or "", Zone.Z1_SRC)

    async def run_challenge_task(challenge: Dict):
        code = challenge.get("code", "unknown")
        display_code = challenge.get("display_code") or challenge.get("title") or code
        runtime_key = challenge.get("runtime_key", code)
        manual_task_id = challenge.get("task_id")
        manual_task = bool(challenge.get("manual_task", False))
        is_forum_task = bool(challenge.get("forum_task", False))
        keep_instance_running = False
        memory_scope_key = challenge.get("memory_scope_key", runtime_key)
        entrypoint = challenge.get("entrypoint") or []
        target_str = ", ".join(entrypoint) if entrypoint else "未启动"
        difficulty = challenge.get("difficulty", "unknown")
        total_score = challenge.get("total_score", 0)
        task_active_map = _active_task_map_for_challenge(challenge)
        strategy_desc = ""
        task_started_at = time.time()
        progress_snapshot: Dict = {}

        def _persist_task_writeup(result: Dict):
            try:
                memory.record_writeup(
                    challenge=challenge,
                    result=result,
                    zone=zone.value if zone else "",
                    scope_key=memory_scope_key,
                    strategy_description=strategy_desc,
                    memory_context=memory_context,
                )
            except Exception as wp_err:
                logger.warning("[WP] 持久化失败: %s | %s", display_code, wp_err)

        async def _enqueue_task_knowledge(result: Dict):
            try:
                # 如果解题成功，先调用顾问进行反思总结
                reflection_summary = ""
                if result.get("success"):
                    try:
                        from agent.reflector import reflect_on_success
                        reflection_summary = await reflect_on_success(
                            challenge=challenge,
                            result=result,
                            model=_main_model,
                        )
                        logger.info("[Knowledge] 顾问反思完成: %s", display_code)
                    except Exception as reflection_err:
                        logger.warning("[Knowledge] 顾问反思失败: %s | %s", display_code, reflection_err)
                        reflection_summary = ""

                enqueue_knowledge_writeback(
                    challenge=challenge,
                    result=result,
                    zone=zone.value if zone else "",
                    scope_key=memory_scope_key,
                    memory_context=memory_context,
                    strategy_description=strategy_desc,
                    reflection_summary=reflection_summary,
                )
            except Exception as knowledge_err:
                logger.warning("[Knowledge] 写回入队失败: %s | %s", display_code, knowledge_err)

        task_semaphore = forum_semaphore if is_forum_task else main_semaphore
        async with task_semaphore:
            queued_codes.discard(runtime_key)
            if runtime_key in aborted_codes:
                scheduler_event.set()
                return
            task_active_map[runtime_key] = asyncio.current_task()
            if (
                not manual_task
                and not is_forum_task
                and scheduler is not None
            ):
                scheduler.active_tasks[code] = asyncio.current_task()
            scheduler_event.set()
            print_challenge_start(code, display_code, difficulty, total_score, target_str)

            zone = None
            if manual_task:
                zone = _zone_from_web_value(challenge.get("zone"))
            elif scheduler is not None:
                zone = scheduler.get_zone_for_challenge(code)
            zone_strategy = scheduler.get_zone_strategy(zone) if (zone and scheduler is not None) else ""
            memory_context = memory.get_context_for_challenge(
                display_code,
                zone.value if zone else "",
                scope_key=memory_scope_key,
                include_shared=not manual_task,
                knowledge_bucket=bucket_for_challenge(challenge),
                challenge=challenge,
            )

            # Web Dashboard 同步
            if enable_web:
                if manual_task_id and get_task_record:
                    rec = get_task_record(manual_task_id)
                else:
                    rec = None

                if rec is None:
                    task_id = f"task_{int(time.time())}_{display_code.lower()}"
                    rec = TaskRecord(task_id, display_code, target_str, difficulty, total_score, zone.value if zone else "")

                rec.challenge_code = display_code
                rec.target = target_str
                rec.difficulty = difficulty
                rec.points = total_score
                rec.zone = zone.value if zone else rec.zone
                rec.status = TaskStatus.RUNNING
                from datetime import datetime
                rec.started_at = datetime.now()
                upsert_task(rec)
                if scheduler is not None and not forum_only_mode:
                    _sync_zones_to_web()

            try:
                allowed_hosts = set()
                for entry in entrypoint:
                    host = (entry or "").strip()
                    if not host:
                        continue
                    if "://" in host:
                        from urllib.parse import urlparse

                        host = urlparse(host).hostname or ""
                    elif ":" in host and host.count(":") == 1:
                        host = host.split(":", 1)[0]
                    host = host.strip()
                    if host:
                        allowed_hosts.add(host)

                current_zone = zone
                retry_count = 0 if (manual_task or scheduler is None) else scheduler.get_retry_count(code)
                retry_level = 1 if (manual_task or scheduler is None) else scheduler.get_retry_level(code)
                enable_swap = config.agent.enable_role_swap_retry
                attempt_history = [] if (manual_task or scheduler is None) else scheduler.get_attempt_history(code)
                last_error_text = str(attempt_history[-1].get("error", "") or "") if attempt_history else ""
                last_infra_failure = _is_infra_failure(last_error_text)

                task_main_llm = forum_main_llm if is_forum_task else main_llm
                task_advisor_llm = forum_advisor_llm if is_forum_task else advisor_llm
                strategy_desc = (
                    f"forum_llm_override({config.llm.forum_llm_provider}:{config.llm.forum_llm_model or config.llm.anthropic_model})"
                    if is_forum_task
                    else "default"
                )
                retry_budget_step = max(0, int(config.agent.retry_attempt_budget_step))
                dynamic_attempt_budget = min(
                    config.agent.max_attempts,
                    max(
                        1,
                        int(config.agent.initial_attempt_budget)
                        + retry_count * retry_budget_step,
                    ),
                )
                # L1 固定 300 轮，L2 固定 100 轮，L3 固定 750 轮，L4 固定 1200 轮
                _challenge_level = challenge.get("level") or (
                    ZONE_INFO.get(zone.value, {}).get("level", 1) if zone else 1
                )
                dynamic_attempt_budget = _challenge_level_turn_budget(
                    int(_challenge_level or 1),
                    dynamic_attempt_budget,
                )
                can_role_swap_non_forum = (
                    bool(enable_swap)
                    and str(config.llm.main_provider or "").strip().lower()
                    == str(config.llm.advisor_provider or "").strip().lower()
                )

                if (not is_forum_task) and retry_count > 0 and last_infra_failure:
                    task_main_llm, task_advisor_llm = advisor_llm, main_llm
                    strategy_desc = "infra_failover(advisor->main)"
                elif (not is_forum_task) and can_role_swap_non_forum and retry_count > 0 and retry_count % 2 == 1:
                    # 奇数次重试角色互换，吸收 CHYing 的高价值策略
                    task_main_llm, task_advisor_llm = advisor_llm, main_llm
                    strategy_desc = "role_swap(advisor->main)"
                elif retry_count > 0:
                    if is_forum_task:
                        strategy_desc += " | retry(forum_main_fixed)"
                    else:
                        strategy_desc = "retry(default_roles)"
                        if enable_swap and not can_role_swap_non_forum:
                            strategy_desc += " | role_swap_disabled(provider_mismatch)"
                if is_forum_task and retry_count > 0 and last_infra_failure:
                    task_main_llm, task_advisor_llm = forum_advisor_llm, forum_main_llm
                    strategy_desc += " | forum_infra_failover(swap-forum-agents)"
                if retry_count > 0:
                    strategy_desc += (
                        f" | retry_count={retry_count} | retry_level=L{retry_level} | "
                        f"attempt_budget={dynamic_attempt_budget}"
                    )

                with scoped_command_policy(
                    allowed_hosts=allowed_hosts,
                    enforce_allowlist=True,
                ):
                    if challenge.get("forum_task") and config.forum.server_host:
                        add_allowed_hosts(
                            [
                                config.forum.server_host,
                                getattr(config.forum, "server_host_fallback", ""),
                            ]
                        )
                    _llm_cfg = config.llm
                    _main_model = getattr(_llm_cfg, "anthropic_model", None) or "claude-opus-4-5"
                    _advisor_model = (
                        getattr(config.agent, "sdk_advisor_model", "")
                        or resolve_advisor_model_name(_llm_cfg)
                        or _main_model
                    )
                    result = await asyncio.wait_for(
                        solve_challenge(
                            challenge=copy.deepcopy(challenge),
                            model=_main_model,
                            advisor_model=_advisor_model,
                            config=config,
                            zone_strategy=zone_strategy,
                            memory_context=memory_context,
                            attempt_history=attempt_history,
                            strategy_description=strategy_desc,
                            max_turns_override=dynamic_attempt_budget,
                            progress_snapshot=progress_snapshot,
                        ),
                        timeout=_challenge_level_task_timeout(
                            int(_challenge_level or 1),
                            config.agent.single_task_timeout,
                            is_forum_task=bool(is_forum_task),
                        ),
                    )

                memory.record_attempt(display_code, result, scope_key=memory_scope_key)
                _persist_task_writeup(result)
                await _enqueue_task_knowledge(result)
                if not manual_task and scheduler is not None:
                    scheduler.record_attempt_result(code, result)

                if result.get("success"):
                    cleanup_status = ""
                    challenge_completed = bool(result.get("challenge_completed", False))
                    if challenge.get("forum_task"):
                        solved_forum_codes.add(code)
                        forum_failed_counts.pop(code, None)
                        forum_cooldown_until.pop(code, None)
                        scored_flags = list(result.get("scored_flags", []) or [])
                        expected_flag_count = int(result.get("expected_flag_count", 1) or 1)
                        if scored_flags:
                            result["flag"] = " | ".join(scored_flags)
                        cleanup_status = (
                            f"论坛模块完成: {result.get('flags_scored_count', len(scored_flags))}/{expected_flag_count}"
                        )
                    elif not manual_task and api_client is not None and scheduler is not None:
                        _zone_level = ZONE_INFO.get(current_zone.value if current_zone else "", {}).get("level", 1)
                        _flag_count = int(result.get("expected_flag_count", challenge.get("flag_count", 1)) or 1)
                        _flag_got = int(result.get("flags_scored_count", challenge.get("flag_got_count", 0)) or 0)
                        if (not challenge_completed) and _should_keep_instance_running_after_success(
                            _zone_level,
                            _flag_got,
                            _flag_count,
                        ):
                            keep_instance_running = True
                            cleanup_status = f"⏳ L3/L4 题目继续运行，已得 {_flag_got}/{_flag_count} flag"
                        else:
                            try:
                                await _run_platform_instance_action("stop_after_success", code, api_client.stop_challenge)
                                scheduler.mark_instance_stopped(code)
                                cleanup_status = "✅ 当前实例已停止"
                            except Exception as stop_err:
                                if "赛题实例未运行" in str(stop_err):
                                    scheduler.mark_instance_stopped(code)
                                cleanup_status = f"⚠️ 当前实例停止失败: {stop_err}"
                                logger.warning("[Task] 成功后停止实例失败: %s | %s", code, stop_err)
                    result["cleanup_status"] = cleanup_status
                    if not manual_task and scheduler is not None and should_mark_challenge_solved(
                        success=bool(result.get("success", False)),
                        challenge_completed=challenge_completed,
                    ):
                        scheduler.mark_solved(code, zone=current_zone)
                        try:
                            await scheduler.refresh_challenges()
                        except Exception as refresh_err:
                            logger.warning("[Task] 成功后刷新赛题失败: %s", refresh_err)
                        # 同步更新前端状态
                        if enable_web:
                            _sync_zones_to_web()
                    _emit_task_result_log(
                        code=code,
                        display_code=display_code,
                        result=result,
                        success=True,
                        cleanup_status=cleanup_status,
                    )
                    print_challenge_result(
                        code,
                        display_code,
                        True,
                        result["attempts"],
                        result["elapsed"],
                        result.get("flag", ""),
                        payloads=result.get("payloads"),
                        action_summary=_summarize_result_path(result),
                        action_history=result.get("action_history"),
                        cleanup_status=cleanup_status,
                    )
                    if enable_web:
                        rec.status = TaskStatus.COMPLETED
                        rec.flag = result.get("flag", "")
                        rec.attempts = result["attempts"]
                        rec.error = ""
                        rec.finished_at = datetime.now()
                        upsert_task(rec)
                else:
                    error_text = str(result.get("error", "") or "")
                    cleanup_status = ""
                    if challenge.get("forum_task"):
                        _mark_runtime_failure(
                            code=code,
                            is_forum_task=True,
                            manual_task=manual_task,
                            error=error_text,
                        )
                    else:
                        _mark_runtime_failure(
                            code=code,
                            is_forum_task=False,
                            manual_task=manual_task,
                            error=error_text,
                        )
                        if not manual_task and api_client is not None and scheduler is not None:
                            cleanup_status = await _cleanup_main_instance_after_failure(code)
                    _emit_task_result_log(
                        code=code,
                        display_code=display_code,
                        result=result,
                        success=False,
                        cleanup_status=cleanup_status,
                    )
                    print_challenge_result(
                        code,
                        display_code,
                        False,
                        result.get("attempts", 0),
                        result.get("elapsed", 0),
                        result.get("flag", ""),
                        payloads=result.get("payloads"),
                        action_summary=_summarize_result_path(result),
                        action_history=result.get("action_history"),
                        cleanup_status=cleanup_status,
                    )
                    if enable_web:
                        rec.status = TaskStatus.FAILED
                        rec.attempts = result.get("attempts", 0)
                        rec.flag = result.get("flag", "")
                        rec.error = result.get("error", "")
                        rec.finished_at = datetime.now()
                        upsert_task(rec)

            except asyncio.TimeoutError:
                timeout_result = _build_timeout_result(
                    started_at=task_started_at,
                    progress_snapshot=progress_snapshot,
                    initial_flag_got_count=int(challenge.get("flag_got_count", 0) or 0),
                    initial_flag_count=int(challenge.get("flag_count", 1) or 1),
                )
                memory.record_attempt(display_code, timeout_result, scope_key=memory_scope_key)
                _persist_task_writeup(timeout_result)
                await _enqueue_task_knowledge(timeout_result)
                if not manual_task and scheduler is not None:
                    scheduler.record_attempt_result(code, timeout_result)
                cleanup_status = ""
                if timeout_result.get("success"):
                    challenge_completed = bool(timeout_result.get("challenge_completed", False))
                    if challenge.get("forum_task"):
                        solved_forum_codes.add(code)
                        forum_failed_counts.pop(code, None)
                        forum_cooldown_until.pop(code, None)
                        scored_flags = list(timeout_result.get("scored_flags", []) or [])
                        expected_flag_count = int(timeout_result.get("expected_flag_count", 1) or 1)
                        if scored_flags:
                            timeout_result["flag"] = " | ".join(scored_flags)
                        cleanup_status = (
                            f"论坛模块进度: {timeout_result.get('flags_scored_count', len(scored_flags))}/{expected_flag_count}"
                        )
                    elif not manual_task and api_client is not None and scheduler is not None:
                        _zone_level = ZONE_INFO.get(current_zone.value if current_zone else "", {}).get("level", 1)
                        _flag_count = int(timeout_result.get("expected_flag_count", challenge.get("flag_count", 1)) or 1)
                        _flag_got = int(timeout_result.get("flags_scored_count", challenge.get("flag_got_count", 0)) or 0)
                        if (not challenge_completed) and _should_keep_instance_running_after_success(
                            _zone_level,
                            _flag_got,
                            _flag_count,
                        ):
                            keep_instance_running = True
                            cleanup_status = f"⏳ 超时前已推进到 {_flag_got}/{_flag_count} flag，当前实例继续保活"
                        else:
                            try:
                                await _run_platform_instance_action("stop_after_success", code, api_client.stop_challenge)
                                scheduler.mark_instance_stopped(code)
                                cleanup_status = "✅ 当前实例已停止"
                            except Exception as stop_err:
                                if "赛题实例未运行" in str(stop_err):
                                    scheduler.mark_instance_stopped(code)
                                cleanup_status = f"⚠️ 当前实例停止失败: {stop_err}"
                                logger.warning("[Task] 超时后按成功路径停止实例失败: %s | %s", code, stop_err)
                    timeout_result["cleanup_status"] = cleanup_status
                    if not manual_task and scheduler is not None and should_mark_challenge_solved(
                        success=bool(timeout_result.get("success", False)),
                        challenge_completed=challenge_completed,
                    ):
                        scheduler.mark_solved(code, zone=current_zone)
                        try:
                            await scheduler.refresh_challenges()
                        except Exception as refresh_err:
                            logger.warning("[Task] 超时后刷新赛题失败: %s", refresh_err)
                        if enable_web:
                            _sync_zones_to_web()
                    _emit_task_result_log(
                        code=code,
                        display_code=display_code,
                        result=timeout_result,
                        success=True,
                        cleanup_status=cleanup_status,
                    )
                    console.print(f"[lingxi.warning]⏰ 超时退出，但本轮已有有效进度: {display_code}[/lingxi.warning]")
                    if cleanup_status:
                        console.print(f"[lingxi.warning]{cleanup_status}[/lingxi.warning]")
                    if enable_web:
                        rec.status = TaskStatus.COMPLETED
                        rec.flag = timeout_result.get("flag", "")
                        rec.attempts = timeout_result.get("attempts", 0)
                        rec.error = timeout_result.get("error", "")
                        rec.finished_at = datetime.now()
                        upsert_task(rec)
                else:
                    _mark_runtime_failure(
                        code=code,
                        is_forum_task=bool(challenge.get("forum_task")),
                        manual_task=manual_task,
                        error="timeout",
                    )
                    if not manual_task and not bool(challenge.get("forum_task")) and api_client is not None and scheduler is not None:
                        cleanup_status = await _cleanup_main_instance_after_failure(code)
                    _emit_task_result_log(
                        code=code,
                        display_code=display_code,
                        result=timeout_result,
                        success=False,
                        cleanup_status=cleanup_status,
                    )
                    console.print(f"[lingxi.warning]⏰ 超时: {display_code}[/lingxi.warning]")
                    if cleanup_status:
                        console.print(f"[lingxi.warning]{cleanup_status}[/lingxi.warning]")
                    if enable_web:
                        rec.status = TaskStatus.FAILED
                        rec.attempts = timeout_result.get("attempts", 0)
                        rec.error = "timeout"
                        rec.finished_at = datetime.now()
                        upsert_task(rec)
            except asyncio.CancelledError:
                cancel_result = {"success": False, "error": "cancelled"}
                memory.record_attempt(display_code, cancel_result, scope_key=memory_scope_key)
                _persist_task_writeup(cancel_result)
                await _enqueue_task_knowledge(cancel_result)
                cleanup_status = ""
                _mark_runtime_failure(
                    code=code,
                    is_forum_task=bool(challenge.get("forum_task")),
                    manual_task=manual_task,
                    error="cancelled",
                )
                if not manual_task and scheduler is not None:
                    scheduler.record_attempt_result(code, cancel_result)
                if not manual_task and not bool(challenge.get("forum_task")) and api_client is not None and scheduler is not None:
                    cleanup_status = await _cleanup_main_instance_after_failure(code)
                _emit_task_result_log(
                    code=code,
                    display_code=display_code,
                    result=cancel_result,
                    success=False,
                    cleanup_status=cleanup_status,
                )
                if cleanup_status:
                    console.print(f"[lingxi.warning]{cleanup_status}[/lingxi.warning]")
                if enable_web:
                    rec.status = TaskStatus.ABORTED if runtime_key in aborted_codes else TaskStatus.PAUSED
                    rec.finished_at = datetime.now()
                    rec.error = "cancelled"
                    upsert_task(rec)
                raise
            except Exception as e:
                err_result = {"success": False, "error": str(e)}
                memory.record_attempt(display_code, err_result, scope_key=memory_scope_key)
                _persist_task_writeup(err_result)
                await _enqueue_task_knowledge(err_result)
                cleanup_status = ""
                _mark_runtime_failure(
                    code=code,
                    is_forum_task=bool(challenge.get("forum_task")),
                    manual_task=manual_task,
                    error=str(e),
                )
                if not manual_task and scheduler is not None:
                    scheduler.record_attempt_result(code, err_result)
                if not manual_task and not bool(challenge.get("forum_task")) and api_client is not None and scheduler is not None:
                    cleanup_status = await _cleanup_main_instance_after_failure(code)
                _emit_task_result_log(
                    code=code,
                    display_code=display_code,
                    result=err_result,
                    success=False,
                    cleanup_status=cleanup_status,
                )
                console.print(f"[lingxi.error]💥 错误: {display_code} | {e}[/lingxi.error]")
                if cleanup_status:
                    console.print(f"[lingxi.warning]{cleanup_status}[/lingxi.warning]")
                if enable_web:
                    rec.status = TaskStatus.FAILED
                    rec.attempts = err_result.get("attempts", 0)
                    rec.flag = err_result.get("flag", "")
                    rec.error = str(e)
                    rec.finished_at = datetime.now()
                    upsert_task(rec)
            finally:
                task_active_map.pop(runtime_key, None)
                if (
                    not manual_task
                    and not is_forum_task
                    and scheduler is not None
                ):
                    scheduler.active_tasks.pop(code, None)
                queued_codes.discard(runtime_key)
                if (
                    not manual_task
                    and not is_forum_task
                    and scheduler is not None
                    and not keep_instance_running
                ):
                    scheduler.running_instances.discard(code)
                if enable_web and scheduler is not None and not forum_only_mode:
                    _sync_zones_to_web()
                scheduler_event.set()

    async def fetch_and_start(
        force_refresh: bool = False,
        refresh_interval_seconds: int | None = None,
    ):
        if scheduler is not None and not forum_only_mode:
            await _ensure_platform_transport()
            if platform_transport_available:
                effective_refresh_interval = int(
                    refresh_interval_seconds or config.agent.fetch_interval_seconds
                )
                if force_refresh or scheduler.need_refresh(effective_refresh_interval):
                    await scheduler.refresh_challenges()
                await _reclaim_completed_main_instances()
                _print_zone_table()

        if config.forum.enabled and (lj_only or dual_run_mode):
            await _ensure_forum_transport()

        # Web 同步赛区
        if enable_web and scheduler is not None and not forum_only_mode:
            _sync_zones_to_web()

        def _pending_sort_key(challenge_obj: Dict):
            startup_priority = challenge_obj.get("_startup_priority")
            if startup_priority is not None:
                return (0, int(startup_priority), 0, 0)
            if scheduler is None:
                code = challenge_obj.get("code", "")
                return (1, 0 if str(code).startswith("forum-") else 1, 0, 0)
            code = challenge_obj.get("code", "")
            zone = scheduler.get_zone_for_challenge(code)
            zone_level = ZONE_INFO.get(zone, {}).get("level", 0) if zone else 0
            retry_count = scheduler.zones[zone].failed.get(code, 0) if zone else 0
            return (1, -zone_level, retry_count, 0)

        async with spawn_lock:
            pending = []
            main_dispatch_budget = 0
            if scheduler is not None and not forum_only_mode and platform_transport_available:
                main_dispatch_budget = _compute_main_dispatch_budget(
                    main_task_limit=main_task_limit,
                    scheduler_active_tasks=scheduler.active_tasks,
                    manual_active_tasks=manual_active_tasks,
                    queued_codes=queued_codes,
                )
                if main_dispatch_budget > 0:
                    pending = scheduler.get_next_challenges(
                        main_dispatch_budget,
                        exclude_codes=queued_codes,
                    )
                else:
                    logger.debug(
                        "[Scheduler] 主战场派发已满: limit=%s active=%s manual=%s queued=%s",
                        main_task_limit,
                        len(scheduler.active_tasks),
                        len(manual_active_tasks),
                        sum(
                            1
                            for runtime_key in queued_codes
                            if not _is_forum_runtime_key(runtime_key)
                        ),
                    )
            enable_forum_autostart = bool(lj_only or dual_run_mode)
            if forum_client is not None and forum_transport_available and enable_forum_autostart:
                forum_pending = _build_forum_challenge_objects()
                forum_inflight = sum(
                    1
                    for task_code in set(forum_active_tasks.keys()) | set(queued_codes)
                    if str(task_code).startswith("forum-")
                )
                forum_budget = max(0, forum_task_limit - forum_inflight)
                for forum_challenge in forum_pending:
                    if forum_budget <= 0:
                        break
                    runtime_key = forum_challenge.get("runtime_key", "")
                    if (
                        runtime_key
                        and runtime_key not in forum_active_tasks
                        and runtime_key not in queued_codes
                    ):
                        pending.append(forum_challenge)
                        forum_budget -= 1
            # 首轮优先当前赛区；重试时优先当前赛区+失败次数低的题
            pending.sort(key=_pending_sort_key)
            remaining_main_dispatch_budget = main_dispatch_budget
            for c in pending:
                code = c.get("code", "")
                runtime_key = _challenge_runtime_key(c)
                active_tasks = _active_task_map_for_challenge(c)
                if runtime_key and runtime_key not in active_tasks and runtime_key not in queued_codes:
                    if not c.get("manual_task") and not c.get("forum_task"):
                        if remaining_main_dispatch_budget <= 0:
                            continue
                        if c.get("instance_status") != "running" or not c.get("entrypoint"):
                            if scheduler is not None and not scheduler.can_start_instance():
                                logger.debug(
                                    "[Scheduler] 实例槽位已满，延后启动: %s | running=%s/%s",
                                    code,
                                    scheduler.get_running_count(),
                                    scheduler.MAX_RUNNING_INSTANCES,
                                )
                                scheduler_event.set()
                                continue
                            try:
                                start_result = await _run_platform_instance_action(
                                    "start",
                                    code,
                                    api_client.start_challenge,
                                )
                                data = start_result.get("data", "")
                                if isinstance(data, dict) and data.get("already_completed"):
                                    scheduler.mark_solved(code, zone=scheduler.get_zone_for_challenge(code))
                                    logger.info("[Scheduler] %s 已完成，跳过启动", code)
                                    scheduler_event.set()
                                    continue
                                if not isinstance(data, list) or not data:
                                    logger.warning("[Scheduler] 启动实例返回异常: %s | %s", code, data)
                                    scheduler.mark_transient_failure(
                                        code,
                                        cooldown_seconds=max(1, config.agent.retry_backoff_seconds),
                                    )
                                    scheduler_event.set()
                                    continue
                                c = dict(c)
                                c["entrypoint"] = data
                                c["instance_status"] = "running"
                                scheduler.mark_instance_started(code, entrypoint=data)
                                await _wait_for_instance_ready(data)
                            except Exception as start_err:
                                logger.warning("[Scheduler] 启动实例失败: %s | %s", code, start_err)
                                scheduler.mark_transient_failure(
                                    code,
                                    cooldown_seconds=max(1, config.agent.retry_backoff_seconds),
                                )
                                scheduler_event.set()
                                continue
                    queued_codes.add(runtime_key)
                    if not c.get("manual_task") and not c.get("forum_task"):
                        remaining_main_dispatch_budget -= 1
                    asyncio.create_task(run_challenge_task(c))
        if pending:
            scheduler_event.set()

    def _build_manual_challenge_from_web(task_id: str, challenge_code: str) -> Dict:
        """将 Web 手工任务转换为可直接执行的 challenge 对象。"""
        if get_task_record:
            rec = get_task_record(task_id)
        else:
            rec = None

        target = ""
        difficulty = "easy"
        total_score = 100

        if rec:
            target = (rec.target or "").strip()
            difficulty = rec.difficulty or difficulty
            total_score = rec.points or total_score

        challenge_text = " ".join(
            item for item in (challenge_code, target, getattr(rec, "zone", "")) if str(item or "").strip()
        )
        level2_hint = resolve_level2_task_hint(task_id, challenge_text=challenge_text)
        zone_value = str((rec.zone if rec else "") or level2_hint.get("zone") or "z1")
        level = 2 if zone_value == "z2" or level2_hint.get("level") == "2" else 1

        entrypoint = []
        if target:
            cleaned = target
            if cleaned.startswith("https://"):
                cleaned = cleaned[len("https://"):]
                if "/" in cleaned:
                    cleaned = cleaned.split("/", 1)[0]
                if ":" not in cleaned:
                    cleaned = f"{cleaned}:443"
            elif cleaned.startswith("http://"):
                cleaned = cleaned[len("http://"):]
                if "/" in cleaned:
                    cleaned = cleaned.split("/", 1)[0]
                if ":" not in cleaned:
                    cleaned = f"{cleaned}:80"
            else:
                if "/" in cleaned:
                    cleaned = cleaned.split("/", 1)[0]
            if cleaned:
                entrypoint = [cleaned]

        manual = {
            "code": challenge_code,
            "display_code": challenge_code,
            "runtime_key": _task_runtime_key(task_id),
            "memory_scope_key": f"manual::{task_id}::{target or challenge_code}",
            "task_id": task_id,
            "title": challenge_code,
            "description": f"Manual Web Task from Dashboard (task_id={task_id})",
            "difficulty": difficulty,
            "total_score": total_score,
            "entrypoint": entrypoint,
            "instance_status": "running",
            "level": level,
            "flag_count": 1,
            "flag_got_count": 0,
            "manual_task": True,
            "zone": zone_value,
            "known_cve": level2_hint.get("cve", ""),
            "preferred_poc_name": level2_hint.get("poc_name", ""),
            "product_hint": level2_hint.get("product", ""),
        }
        return manual

    async def _web_on_start_task(task_id: str, challenge_code: str):
        """Web: 创建任务后立即派发执行。"""
        if not enable_web:
            return
        challenge = _build_manual_challenge_from_web(task_id, challenge_code)
        runtime_key = challenge["runtime_key"]
        aborted_codes.discard(runtime_key)
        rec = get_task_record(task_id) if get_task_record else None
        if rec:
            rec.status = TaskStatus.RUNNING
            from datetime import datetime
            rec.started_at = datetime.now()
            upsert_task(rec)
        if runtime_key not in manual_active_tasks and runtime_key not in queued_codes:
            queued_codes.add(runtime_key)
            asyncio.create_task(run_challenge_task(challenge))
            scheduler_event.set()
            if push_log:
                push_log("info", f"Task dispatched: {challenge_code}", "web")

    async def _web_on_abort_task(task_id: str):
        """Web: 中止任务，取消正在执行的协程。"""
        if not get_task_record:
            return
        rec = get_task_record(task_id)
        if not rec:
            return
        code = rec.challenge_code
        runtime_key = _task_runtime_key(task_id)
        aborted_codes.add(runtime_key)
        task = manual_active_tasks.get(runtime_key)
        if task and not task.done():
            task.cancel()
        queued_codes.discard(runtime_key)
        scheduler_event.set()
        if push_log:
            push_log("warn", f"Task aborted and cancelled: {code}", "web")

    async def _web_on_pause_task(task_id: str):
        """Web: 暂停任务，实作为取消当前执行。"""
        if not get_task_record:
            return
        rec = get_task_record(task_id)
        if not rec:
            return
        code = rec.challenge_code
        runtime_key = _task_runtime_key(task_id)
        task = manual_active_tasks.get(runtime_key)
        if task and not task.done():
            task.cancel()
        queued_codes.discard(runtime_key)
        scheduler_event.set()
        if push_log:
            push_log("warn", f"Task paused (cancel current run): {code}", "web")

    async def _web_on_resume_task(task_id: str):
        """Web: 恢复任务，重新派发。"""
        if not get_task_record:
            return
        rec = get_task_record(task_id)
        if not rec:
            return
        code = rec.challenge_code
        runtime_key = _task_runtime_key(task_id)
        aborted_codes.discard(runtime_key)
        challenge = _build_manual_challenge_from_web(task_id, code)
        if runtime_key not in manual_active_tasks and runtime_key not in queued_codes:
            queued_codes.add(runtime_key)
            asyncio.create_task(run_challenge_task(challenge))
            scheduler_event.set()
            if push_log:
                push_log("info", f"Task resumed and dispatched: {code}", "web")

    def _print_zone_table(force: bool = False):
        nonlocal last_zone_table_signature
        if scheduler is None:
            return
        data = []
        for z in Zone:
            s = scheduler.zones[z]
            info = ZONE_INFO[z]
            data.append((info["name"], s.unlocked, len(s.solved), len(s.challenges), s.total_score))
        signature = tuple(data)
        if not force and signature == last_zone_table_signature:
            return
        last_zone_table_signature = signature
        print_zone_status(data)

    def _sync_zones_to_web():
        if not enable_web or scheduler is None:
            return
        zones_data = []
        for z in Zone:
            s = scheduler.zones[z]
            info = ZONE_INFO[z]
            zones_data.append({
                "name": info["name"],
                "unlocked": s.unlocked,
                "solved": len(s.solved),
                "total": len(s.challenges),
                "excluded_total": int(getattr(s, "excluded_total", 0) or 0),
                "demo_skipped": int(getattr(s, "demo_skipped", 0) or 0),
                "score": s.total_score,
            })
        update_zones(zones_data)
        update_agent_state({
            "status": "running",
            "total_solved": sum(len(s.solved) for s in scheduler.zones.values()),
            "total_score": sum(s.total_score for s in scheduler.zones.values()),
        })

    async def status_monitor():
        while True:
            await asyncio.sleep(300)
            if scheduler is not None and not lj_only and not platform_transport_available:
                await _ensure_platform_transport()
            if config.forum.enabled and not forum_transport_available:
                await _ensure_forum_transport()
            _print_zone_table(force=True)
            if enable_web and scheduler is not None:
                _sync_zones_to_web()
            active_count = sum(len(task_map) for task_map in _all_active_task_maps())
            console.print(f"  [dim]活跃任务: {active_count}[/dim]")

    async def scheduler_loop():
        while True:
            try:
                # 检测主攻手是否空闲（没有活跃任务）
                main_active_count = len(scheduler.active_tasks) if scheduler is not None else 0
                is_idle = main_active_count == 0
                refresh_interval = _scheduler_refresh_interval_seconds(is_idle=is_idle)
                if is_idle and scheduler is not None and scheduler.need_refresh(refresh_interval):
                    logger.debug(
                        "[SchedulerLoop] 主攻手空闲，按 %ss 节奏主动刷新题目列表",
                        refresh_interval,
                    )
                await fetch_and_start(
                    force_refresh=False,
                    refresh_interval_seconds=refresh_interval,
                )
            except Exception as e:
                logger.exception("[SchedulerLoop] 调度异常: %s", e)

            if _should_consume_scheduler_event(scheduler_event):
                continue

            try:
                loop_wait = _scheduler_refresh_interval_seconds(
                    is_idle=(len(scheduler.active_tasks) if scheduler is not None else 0) == 0
                )
                await asyncio.wait_for(scheduler_event.wait(), timeout=loop_wait)
            except asyncio.TimeoutError:
                pass

    if enable_web and register_callbacks:
        register_callbacks(
            on_start_task=_web_on_start_task,
            on_pause_task=_web_on_pause_task,
            on_abort_task=_web_on_abort_task,
            on_resume_task=_web_on_resume_task,
        )

    async def wait_for_challenges_with_retry(retry_interval: int = 5, max_retries: int = 0):
        """
        持续重试拉取题目直到成功

        Args:
            retry_interval: 重试间隔（秒）
            max_retries: 最大重试次数，0表示无限重试
        """
        retry_count = 0
        while True:
            try:
                if forum_only_mode:
                    if not forum_transport_available:
                        if not await _ensure_forum_transport():
                            raise Exception("论坛 API/MCP 连接失败")
                    # 尝试拉取论坛题目
                    forum_challenges = _build_forum_challenge_objects()
                    if forum_challenges:
                        console.print(f"[lingxi.success]✅ 成功获取 {len(forum_challenges)} 道论坛题目[/lingxi.success]")
                        return True
                    else:
                        raise Exception("论坛题目列表为空")
                else:
                    if not platform_transport_available:
                        if not await _ensure_platform_transport():
                            raise Exception("平台 API 连接失败")
                    # 尝试拉取主战场题目
                    if scheduler is not None:
                        challenges = await scheduler.refresh_challenges()
                        if challenges:
                            console.print(f"[lingxi.success]✅ 成功获取 {len(challenges)} 道题目[/lingxi.success]")
                            return True
                        else:
                            raise Exception("题目列表为空，可能环境尚未就绪")
                    else:
                        raise Exception("调度器未初始化")
            except Exception as e:
                retry_count += 1
                if max_retries > 0 and retry_count >= max_retries:
                    console.print(f"[lingxi.error]❌ 达到最大重试次数 ({max_retries})，拉取题目失败: {e}[/lingxi.error]")
                    return False

                console.print(f"[lingxi.warning]⚠️  拉取题目失败 (第{retry_count}次): {e}[/lingxi.warning]")
                console.print(f"[lingxi.info]🔄 {retry_interval}秒后重试...[/lingxi.info]")
                await asyncio.sleep(retry_interval)

    # ─── 启动 ───
    if forum_only_mode:
        console.print("[lingxi.info]🔍 正在获取灵境/零界题目...[/lingxi.info]")
        console.print(f"[lingxi.info]💡 提示: 如果环境尚未就绪，将每{_IDLE_FETCH_SECONDS}秒自动重试[/lingxi.info]")

        if enable_web:
            update_agent_state({"status": "running", "start_time": int(time.time())})
            push_log("info", "Ling-Xi Agent 启动（--lj）", "system")

        # 持续重试直到成功
        if not await wait_for_challenges_with_retry(retry_interval=_IDLE_FETCH_SECONDS):
            console.print("[lingxi.error]❌ --lj 模式需要可用的零界论坛 API/MCP[/lingxi.error]")
            sys.exit(1)

        await fetch_and_start(force_refresh=True)
        fetch_task = asyncio.create_task(scheduler_loop())
        monitor_task = asyncio.create_task(status_monitor())
    elif config.platform.api_base_url:
        if dual_run_mode:
            console.print("[lingxi.info]🔍 正在获取主战场与灵境/零界题目...[/lingxi.info]")
        else:
            console.print("[lingxi.info]🔍 正在获取题目...[/lingxi.info]")
        console.print(f"[lingxi.info]💡 提示: 如果环境尚未就绪，将每{_IDLE_FETCH_SECONDS}秒自动重试[/lingxi.info]")

        if enable_web:
            update_agent_state({"status": "running", "start_time": int(time.time())})
            if dual_run_mode:
                push_log("info", "Ling-Xi Agent 启动（--all 双开）", "system")
            else:
                push_log("info", "Ling-Xi Agent 启动", "system")

        # 持续重试直到成功
        if not await wait_for_challenges_with_retry(retry_interval=_IDLE_FETCH_SECONDS):
            console.print("[lingxi.error]❌ 拉取题目失败，请检查平台配置[/lingxi.error]")
            sys.exit(1)

        if dual_run_mode:
            await _ensure_forum_transport()
        await fetch_and_start(force_refresh=True)
        fetch_task = asyncio.create_task(scheduler_loop())
        monitor_task = asyncio.create_task(status_monitor())
    else:
        console.print("[lingxi.warning]⚠️  未配置比赛平台 URL，进入待命模式（Dashboard 可用）[/lingxi.warning]")

        if enable_web:
            update_agent_state({"status": "idle", "start_time": int(time.time())})
            push_log("info", "Ling-Xi 待命模式 — 等待配置比赛平台", "system")
            _sync_zones_to_web()

        fetch_task = asyncio.create_task(asyncio.sleep(999999))
        monitor_task = asyncio.create_task(asyncio.sleep(999999))

    console.rule("[bold bright_cyan]Ling-Xi 运行中 — Ctrl+C 停止[/bold bright_cyan]")
    console.print()

    try:
        stop_reason = ""
        while not stop_reason:
            done, _ = await asyncio.wait(
                {fetch_task, monitor_task, shutdown_future},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_future in done:
                stop_reason = shutdown_future.result() or "signal"
                break
            for task_name, task in (("fetch", fetch_task), ("monitor", monitor_task)):
                if task not in done:
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
                stop_reason = f"{task_name}_completed"
                logger.warning("[Main] 后台任务意外结束: %s", task_name)
                break

        console.print("\n[lingxi.warning]🛑 正在关闭...[/lingxi.warning]")
    except KeyboardInterrupt:
        _request_shutdown("keyboard_interrupt")
        console.print("\n[lingxi.warning]🛑 正在关闭...[/lingxi.warning]")
    finally:
        shutdown_reason = shutdown_future.result() if shutdown_future.done() else "finalize"
        logger.info("[Main] 开始停机收尾: reason=%s", shutdown_reason)

        fetch_task.cancel()
        monitor_task.cancel()
        active_task_candidates: list[asyncio.Task | None] = [fetch_task, monitor_task]
        for active_tasks in _all_active_task_maps():
            for task in active_tasks.values():
                active_task_candidates.append(task)
                task.cancel()

        sdk_summary = await shutdown_active_sdk_sessions()
        if sdk_summary["attempted"] > 0:
            logger.info(
                "[Main] SDK同步关闭摘要: attempted=%s closed=%s timed_out=%s failed=%s skipped=%s",
                sdk_summary["attempted"],
                sdk_summary["closed"],
                sdk_summary["timed_out"],
                sdk_summary["failed"],
                sdk_summary["skipped"],
            )
        task_shutdown_summary = await _wait_for_task_shutdown(active_task_candidates)
        if task_shutdown_summary["awaited"] > 0:
            logger.info(
                "[Main] 任务关闭摘要: awaited=%s timed_out=%s",
                task_shutdown_summary["awaited"],
                task_shutdown_summary["timed_out"],
            )

        if scheduler is not None:
            total_solved = sum(len(s.solved) for s in scheduler.zones.values())
            total_score = sum(s.total_score for s in scheduler.zones.values())
        else:
            total_solved = len(solved_forum_codes)
            total_score = 0
        elapsed = time.time() - start_time
        _print_zone_table(force=True)
        print_final_report(total_solved, total_score, elapsed)

        if enable_web:
            update_agent_state({"status": "idle"})
        if forum_message_state_task is not None:
            forum_message_state_task.cancel()
            try:
                await forum_message_state_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[Main] 关闭论坛私信状态机失败: %s", exc)
        if knowledge_writeback_task is not None:
            knowledge_writeback_task.cancel()
            try:
                await knowledge_writeback_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[Main] 关闭知识写回 worker 失败: %s", exc)
        try:
            shutdown_forum_mcp()
        except Exception as exc:
            logger.warning("[Main] 关闭论坛 MCP 失败: %s", exc)
        try:
            shutdown_sliver_mcp()
        except Exception as exc:
            logger.warning("[Main] 关闭 Sliver MCP 失败: %s", exc)
        try:
            shutdown_kali_mcp()
        except Exception as exc:
            logger.warning("[Main] 关闭 Kali MCP 失败: %s", exc)
        for sig in installed_signal_handlers:
            with suppress(Exception):
                loop.remove_signal_handler(sig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ling-Xi — 自主渗透测试智能体")
    parser.add_argument("--web", action="store_true", help="启动 Web Dashboard")
    parser.add_argument("--port", type=int, default=8899, help="Dashboard 端口 (默认 8899)")
    parser.add_argument("--main", action="store_true", help="启动主战场模式")
    parser.add_argument("--lj", action="store_true", help="仅启动灵境/零界论坛题目模式")
    parser.add_argument("--all", action="store_true", help="主战场 + 灵境/零界双开")

    # 独立模式参数
    parser.add_argument("--web-only", action="store_true", help="仅启动 Web Dashboard (独立进程)")
    parser.add_argument("--main-only", action="store_true", help="仅启动主战场 (独立进程)")
    parser.add_argument("--forum-only", action="store_true", help="仅启动论坛 (独立进程)")

    args = parser.parse_args()

    # 参数冲突检查
    mode_flags = [args.main, args.lj, args.all, args.web_only, args.main_only, args.forum_only]
    if sum(mode_flags) > 1:
        parser.error("--main, --lj, --all, --web-only, --main-only, --forum-only 只能选择一个")

    # 独立模式处理
    if args.web_only:
        # 仅启动Web Dashboard
        asyncio.run(main(enable_web=True, web_port=args.port, lj_only=False, run_all=False))
    elif args.main_only:
        # 仅启动主战场
        asyncio.run(main(enable_web=False, web_port=args.port, lj_only=False, run_all=False))
    elif args.forum_only:
        # 仅启动论坛
        asyncio.run(main(enable_web=False, web_port=args.port, lj_only=True, run_all=False))
    else:
        # 默认行为：如果没有指定任何模式，使用主战场模式
        if not any(mode_flags):
            args.main = True

        asyncio.run(
            main(
                enable_web=args.web,
                web_port=args.port,
                lj_only=args.lj,
                run_all=args.all,
            )
        )
