from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


if "langchain_core.tools" not in sys.modules:
    langchain_core_stub = ModuleType("langchain_core")
    langchain_tools_stub = ModuleType("langchain_core.tools")

    def _tool(*args, **kwargs):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            fn = args[0]
            fn.name = fn.__name__
            return fn

        explicit_name = args[0] if args and isinstance(args[0], str) else kwargs.get("name")

        def decorator(fn):
            fn.name = explicit_name or fn.__name__
            return fn

        return decorator

    langchain_tools_stub.tool = _tool
    langchain_core_stub.tools = langchain_tools_stub
    sys.modules["langchain_core"] = langchain_core_stub
    sys.modules["langchain_core.tools"] = langchain_tools_stub

if "claude_code_sdk" not in sys.modules:
    sdk_stub = ModuleType("claude_code_sdk")
    sdk_stub.query = lambda *args, **kwargs: None
    sdk_stub.ClaudeCodeOptions = type("ClaudeCodeOptions", (), {})
    sys.modules["claude_code_sdk"] = sdk_stub

    client_stub = ModuleType("claude_code_sdk.client")
    client_stub.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
    sys.modules["claude_code_sdk.client"] = client_stub

    types_stub = ModuleType("claude_code_sdk.types")
    for name in (
        "AssistantMessage",
        "SystemMessage",
        "UserMessage",
        "ResultMessage",
        "TextBlock",
        "ToolUseBlock",
        "ToolResultBlock",
        "PermissionResultAllow",
        "PermissionResultDeny",
        "ToolPermissionContext",
    ):
        setattr(types_stub, name, type(name, (), {}))
    sys.modules["claude_code_sdk.types"] = types_stub

from agent.scheduler import Zone, ZoneScheduler
from agent.sdk_runner import (
    _build_advisor_sdk_env,
    build_mcp_servers,
    get_active_sdk_handle_count,
    shutdown_active_sdk_sessions,
)
from config import DockerConfig, ForumConfig, PlatformConfig, LLMConfig, resolve_advisor_model_name
from kali_container import DEFAULT_KALI_CONTAINER_NAME, get_kali_container_name
from main import (
    _build_timeout_result,
    _emit_task_result_log,
    _is_infra_failure,
    _is_platform_transition_conflict,
    _summarize_result_path,
    _should_consume_scheduler_event,
    _should_keep_instance_running_after_success,
)
from runtime_env import get_project_python
from tools.platform_api import CompetitionAPIClient
from tools.shell import configure_shell


class RuntimeConfigTests(unittest.TestCase):
    def test_get_project_python_falls_back_to_venv_when_dotvenv_missing(self) -> None:
        root = Path("/tmp/demo-project")
        venv_python = root / "venv" / "bin" / "python"
        dotvenv_python = root / ".venv" / "bin" / "python"

        with patch("runtime_env.get_project_root", return_value=root):
            get_project_python.cache_clear()
            try:
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    Path,
                    "exists",
                    autospec=True,
                    side_effect=lambda self: self in {venv_python},
                ):
                    self.assertEqual(str(venv_python), get_project_python())
            finally:
                get_project_python.cache_clear()

    def test_server_host_only_affects_forum_config(self) -> None:
        env = {
            "SERVER_HOST": "https://forum.example",
            "COMPETITION_BASE_URL": "https://arena.example",
            "COMPETITION_API_BASE_URL": "https://api.arena.example",
        }
        with patch.dict(os.environ, env, clear=False):
            platform = PlatformConfig()
            forum = ForumConfig()

        self.assertEqual("https://arena.example", platform.base_url)
        self.assertEqual("https://api.arena.example", platform.api_base_url)
        self.assertEqual("https://forum.example", forum.server_host)

    def test_platform_api_client_does_not_fallback_to_server_host(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SERVER_HOST": "https://forum.example",
            },
            clear=True,
        ):
            client = CompetitionAPIClient()

        self.assertEqual("", client.base_url)

    def test_platform_config_derives_competition_server_host_from_api_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "COMPETITION_API_BASE_URL": "https://arena.example",
                "COMPETITION_SERVER_HOST_FALLBACK": "fallback.example:8000",
            },
            clear=True,
        ):
            platform = PlatformConfig()

        self.assertEqual("https://arena.example", platform.api_base_url)
        self.assertEqual("https://arena.example", platform.server_host)
        self.assertEqual("http://fallback.example:8000", platform.server_host_fallback)

    def test_platform_config_prefers_explicit_competition_server_host(self) -> None:
        with patch.dict(
            os.environ,
            {
                "COMPETITION_API_BASE_URL": "https://arena.example",
                "COMPETITION_SERVER_HOST": "https://platform-direct.example:9000",
                "COMPETITION_SERVER_HOST_FALLBACK": "https://fallback.example:8000",
            },
            clear=True,
        ):
            platform = PlatformConfig()

        self.assertEqual("https://platform-direct.example:9000", platform.server_host)
        self.assertEqual("https://fallback.example:8000", platform.server_host_fallback)

    def test_docker_config_falls_back_to_unique_running_kali_container_when_env_is_stale(self) -> None:
        with patch.dict(
            os.environ,
            {"DOCKER_CONTAINER_NAME": DEFAULT_KALI_CONTAINER_NAME},
            clear=True,
        ), patch(
            "kali_container.list_running_docker_container_names",
            return_value=["redis", "kali-research"],
        ):
            docker = DockerConfig()

        self.assertEqual("kali-research", docker.container_name)

    def test_get_kali_container_name_uses_default_when_no_env_and_no_running_container(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "kali_container.list_running_docker_container_names",
            return_value=[],
        ):
            name = get_kali_container_name()

        self.assertEqual(DEFAULT_KALI_CONTAINER_NAME, name)

    def test_configure_shell_uses_shared_container_resolution(self) -> None:
        with patch("tools.shell.get_kali_container_name", return_value="kali-research"), patch(
            "tools.shell._docker_container_exists",
            return_value=True,
        ):
            state = configure_shell("", True)

        self.assertEqual("docker", state["mode"])
        self.assertEqual("kali-research", state["container"])

    def test_build_advisor_sdk_env_maps_advisor_anthropic_vars_to_sdk_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ADVISOR_LLM_PROVIDER": "anthropic",
                "ADVISOR_ANTHROPIC_BASE_URL": "https://advisor-gateway.example/anthropic",
                "ADVISOR_ANTHROPIC_API_KEY": "advisor-demo-key",
                "ADVISOR_ANTHROPIC_MODEL": "claude-opus-advisor",
                "SDK_ADVISOR_MODEL": "",
                "ANTHROPIC_BASE_URL": "https://main-gateway.example/anthropic",
                "ANTHROPIC_API_KEY": "main-demo-key",
                "ANTHROPIC_MODEL": "claude-opus-main",
            },
            clear=False,
        ):
            env = _build_advisor_sdk_env()

        self.assertEqual("https://advisor-gateway.example/anthropic", env["ANTHROPIC_BASE_URL"])
        self.assertEqual("advisor-demo-key", env["ANTHROPIC_API_KEY"])
        self.assertEqual("claude-opus-advisor", env["ANTHROPIC_MODEL"])

    def test_build_advisor_sdk_env_maps_deepseek_vars_when_advisor_provider_is_deepseek(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ADVISOR_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_BASE_URL": "https://advisor-gateway.example/deepseek",
                "DEEPSEEK_API_KEY": "deepseek-demo-key",
                "DEEPSEEK_MODEL": "deepseek-r1",
                "SDK_ADVISOR_MODEL": "",
            },
            clear=False,
        ):
            env = _build_advisor_sdk_env()

        self.assertEqual("https://advisor-gateway.example/deepseek", env["ANTHROPIC_BASE_URL"])
        self.assertEqual("deepseek-demo-key", env["ANTHROPIC_API_KEY"])
        self.assertEqual("deepseek-r1", env["ANTHROPIC_MODEL"])

    def test_resolve_advisor_model_name_follows_advisor_provider(self) -> None:
        llm_cfg = LLMConfig()
        llm_cfg.advisor_provider = "deepseek"
        llm_cfg.deepseek_model = "deepseek-ai/DeepSeek-R1"
        llm_cfg.advisor_anthropic_model = "claude-opus-advisor"

        self.assertEqual("deepseek-ai/DeepSeek-R1", resolve_advisor_model_name(llm_cfg))


class MainLifecycleHelperTests(unittest.TestCase):
    def test_keep_instance_running_only_for_high_level_partial_success(self) -> None:
        self.assertFalse(_should_keep_instance_running_after_success(1, 1, 1))
        self.assertFalse(_should_keep_instance_running_after_success(2, 1, 1))
        self.assertTrue(_should_keep_instance_running_after_success(3, 1, 2))
        self.assertTrue(_should_keep_instance_running_after_success(4, 2, 3))
        self.assertFalse(_should_keep_instance_running_after_success(4, 3, 3))

    def test_build_timeout_result_preserves_progress_snapshot(self) -> None:
        with patch("main.time.time", return_value=15.3):
            result = _build_timeout_result(
                started_at=10.0,
                progress_snapshot={
                    "attempts": 7,
                    "current_strategy": "verify admin login flow",
                    "action_history": ["step-1", "step-2"],
                    "payloads": ["curl /login"],
                    "advisor_call_count": 1,
                },
            )

        self.assertEqual(7, result["attempts"])
        self.assertEqual(5.3, result["elapsed"])
        self.assertEqual("verify admin login flow", result["final_strategy"])
        self.assertEqual(["step-1", "step-2"], result["action_history"])
        self.assertEqual(["curl /login"], result["payloads"])

    def test_build_timeout_result_marks_partial_multi_flag_progress_as_success(self) -> None:
        with patch("main.time.time", return_value=21.0):
            result = _build_timeout_result(
                started_at=10.0,
                initial_flag_got_count=1,
                initial_flag_count=4,
                progress_snapshot={
                    "flags_scored_count": 2,
                    "expected_flag_count": 4,
                    "scored_flags": ["flag{a}", "flag{b}"],
                },
            )

        self.assertTrue(result["success"])
        self.assertEqual("timeout_after_partial_progress", result["error"])
        self.assertEqual(2, result["flags_scored_count"])
        self.assertEqual(4, result["expected_flag_count"])
        self.assertFalse(result["challenge_completed"])
        self.assertFalse(result["is_finished"])

    def test_build_mcp_servers_enables_sliver_for_level3_challenge(self) -> None:
        cfg = SimpleNamespace(
            sliver=SimpleNamespace(
                client_path="./bin/sliver-client",
                client_config_path="./sliver-config",
            ),
            docker=SimpleNamespace(
                container_name="kali-research",
            ),
        )
        with patch("config.load_config", return_value=cfg), patch(
            "agent.sdk_runner.os.path.exists",
            return_value=True,
        ):
            servers = build_mcp_servers({"code": "z3-demo", "level": 3})

        self.assertIn("sliver", servers)
        self.assertIn("kali", servers)
        self.assertEqual("docker", servers["kali"]["command"])
        self.assertIn("kali-research", servers["kali"]["args"])

    def test_scheduler_record_attempt_result_updates_cached_flag_progress(self) -> None:
        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                attempt_history_limit=3,
                max_retries=4,
                retry_backoff_seconds=60,
            )
        )
        scheduler = ZoneScheduler(MagicMock(), cfg)
        zone = Zone.Z3_NETWORK
        challenge = {
            "code": "corp-1",
            "flag_count": 4,
            "flag_got_count": 0,
            "instance_status": "running",
        }
        scheduler.zones[zone].challenges.append(challenge)
        scheduler._challenge_zone_index["corp-1"] = zone

        scheduler.record_attempt_result(
            "corp-1",
            {
                "success": True,
                "flags_scored_count": 2,
                "expected_flag_count": 4,
            },
        )

        self.assertEqual(2, challenge["flag_got_count"])
        self.assertEqual(4, challenge["flag_count"])
        self.assertNotIn("corp-1", scheduler.zones[zone].solved)

    def test_consume_scheduler_event_clears_only_when_set(self) -> None:
        event = asyncio.Event()
        self.assertFalse(_should_consume_scheduler_event(event))
        self.assertFalse(event.is_set())

        event.set()
        self.assertTrue(_should_consume_scheduler_event(event))
        self.assertFalse(event.is_set())

    def test_platform_transition_cooldown_uses_longer_window_for_busy_state(self) -> None:
        def cooldown(schedule_tick_seconds: int, *, busy: bool = False) -> float:
            base = max(1.0, min(10.0, float(schedule_tick_seconds)))
            return max(base, 5.0) if busy else base

        self.assertEqual(3.0, cooldown(3, busy=False))
        self.assertEqual(5.0, cooldown(3, busy=True))
        self.assertEqual(10.0, cooldown(12, busy=False))
        self.assertEqual(10.0, cooldown(12, busy=True))

    def test_infra_failure_detects_sdk_cancel_scope_and_jsonrpc_transport_errors(self) -> None:
        self.assertTrue(_is_infra_failure("API Error: 503 请求错误(状态码: 503)"))
        self.assertTrue(
            _is_infra_failure(
                "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope"
            )
        )
        self.assertTrue(_is_infra_failure("Failed to parse JSONRPC message from server"))
        self.assertFalse(_is_infra_failure("flag format invalid"))

    def test_platform_transition_conflict_detects_instance_start_stop_races(self) -> None:
        self.assertTrue(_is_platform_transition_conflict("HTTP 409: 已有实例正在启动或停止中，请稍后重试"))
        self.assertTrue(_is_platform_transition_conflict("赛题实例正在停止中"))
        self.assertFalse(_is_platform_transition_conflict("HTTP 400: 赛题实例未运行"))

    def test_platform_transition_busy_error_detection_matches_current_409_text(self) -> None:
        busy_text = "HTTP 409: 已有实例正在启动或停止中，请稍后重试"
        other_text = "HTTP 400: 最多同时运行3个实例，请先停止其他实例"

        def is_busy_transition_error(message: str) -> bool:
            return (
                "正在启动中" in message
                or "正在停止中" in message
                or "已有实例正在启动或停止中" in message
            )

        self.assertTrue(is_busy_transition_error(busy_text))
        self.assertFalse(is_busy_transition_error(other_text))

    def test_result_summary_prefers_strategy_then_actions_then_error(self) -> None:
        self.assertEqual(
            "check docs then login",
            _summarize_result_path({"final_strategy": "check docs then login", "action_history": ["a", "b"]}),
        )
        self.assertIn(
            "发现 /docs",
            _summarize_result_path({"action_history": ["发现 /docs", "读取 openapi.json", "使用默认口令登录"]}),
        )
        self.assertEqual(
            "error=timeout",
            _summarize_result_path({"error": "timeout"}),
        )

    def test_emit_task_result_log_records_structured_summary(self) -> None:
        with self.assertLogs("lingxi", level="INFO") as cm:
            _emit_task_result_log(
                code="web-1",
                display_code="Web One",
                result={
                    "attempts": 3,
                    "elapsed": 42.0,
                    "flag": "flag{demo-secret}",
                    "scored_flags": ["flag{demo-secret}"],
                    "final_strategy": "check docs then login",
                    "thought_summary": "先看 docs，再登录后台拿 flag",
                    "payloads": ["execute_python | requests.get('https://target.example/admin')"],
                    "action_history": ["发现 /docs", "登录后台成功"],
                    "advisor_call_count": 1,
                    "advisor_summary": "advisor#1 reason=no_tool_rounds=2 suggestion=先查看 /docs 暴露的接口",
                    "knowledge_call_count": 1,
                    "knowledge_summary": "kb#1 reason=no_tool_rounds=2 sources=main_memory hit=yes payload=主战场记忆",
                    "system_prompt_excerpt": "system prompt payload",
                    "initial_prompt_excerpt": "user prompt payload",
                    "memory_context_excerpt": "memory excerpt",
                    "skill_context_excerpt": "skill excerpt",
                },
                success=True,
                cleanup_status="done",
            )

        output = "\n".join(cm.output)
        self.assertIn("TaskSummary", output)
        self.assertIn("TaskDetail", output)
        self.assertIn("check docs then login", output)
        self.assertIn("先看 docs，再登录后台拿 flag", output)
        self.assertIn("execute_python", output)
        self.assertIn("登录后台成功", output)
        self.assertIn("system prompt payload", output)
        self.assertIn("advisor#1", output)

    def test_shutdown_active_sdk_sessions_closes_registered_handles(self) -> None:
        class DummyClient:
            def __init__(self) -> None:
                self.closed = 0

            async def __aexit__(self, exc_type, exc, tb) -> None:
                self.closed += 1

        class DummyStream:
            def __init__(self) -> None:
                self.closed = 0

            async def aclose(self) -> None:
                self.closed += 1

        from agent import sdk_runner

        client = DummyClient()
        stream = DummyStream()
        sdk_runner._register_sdk_handle(client, label="client", challenge_code="main-1")
        sdk_runner._register_sdk_handle(stream, label="advisor_stream", challenge_code="main-1")
        self.assertEqual(2, get_active_sdk_handle_count())

        summary = asyncio.run(shutdown_active_sdk_sessions(timeout=1.0))

        self.assertEqual(2, summary["attempted"])
        self.assertEqual(2, summary["closed"])
        self.assertEqual(0, summary["timed_out"])
        self.assertEqual(0, summary["failed"])
        self.assertEqual(1, client.closed)
        self.assertEqual(1, stream.closed)
        self.assertEqual(0, get_active_sdk_handle_count())


class SchedulerDemoWhitelistTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_challenges_excludes_non_allowlisted_demo_tasks(self) -> None:
        payload = {
            "current_level": 1,
            "challenges": [
                {
                    "code": "demo-1",
                    "display_code": "welcome to demo1",
                    "title": "welcome to demo1",
                    "level": 1,
                    "difficulty": "easy",
                    "flag_count": 1,
                    "flag_got_count": 0,
                },
                {
                    "code": "real-web-1",
                    "display_code": "real-web-1",
                    "title": "real web 1",
                    "level": 1,
                    "difficulty": "easy",
                    "flag_count": 1,
                    "flag_got_count": 0,
                },
                {
                    "code": "demo-keep",
                    "display_code": "demo-keep",
                    "title": "demo keep-me please",
                    "level": 1,
                    "difficulty": "easy",
                    "flag_count": 1,
                    "flag_got_count": 0,
                },
            ],
        }
        scheduler = ZoneScheduler(MagicMock(), config=None)
        with patch.dict(os.environ, {"MAIN_BATTLE_DEMO_ALLOWLIST": "keep-me"}, clear=False), patch(
            "agent.scheduler.run_platform_api_io",
            new=AsyncMock(return_value=payload),
        ):
            await scheduler.refresh_challenges()

        zone = scheduler.zones[Zone.Z1_SRC]
        self.assertEqual(["real-web-1", "demo-keep"], [item["code"] for item in zone.challenges])
        self.assertEqual(1, zone.excluded_total)
        self.assertEqual(1, zone.demo_skipped)
        self.assertEqual(
            ["real-web-1", "demo-keep"],
            [item["code"] for item in scheduler.get_next_challenges(max_count=10)],
        )

    async def test_demo_match_uses_code_title_and_display_code(self) -> None:
        payload = {
            "current_level": 1,
            "challenges": [
                {
                    "code": "src-100",
                    "display_code": "Friendly demo showcase",
                    "title": "not-demo-code",
                    "level": 1,
                    "difficulty": "easy",
                },
                {
                    "code": "src-101",
                    "display_code": "real-display",
                    "title": "Real Challenge",
                    "level": 1,
                    "difficulty": "easy",
                },
            ],
        }
        scheduler = ZoneScheduler(MagicMock(), config=None)
        with patch.dict(os.environ, {"MAIN_BATTLE_DEMO_ALLOWLIST": ""}, clear=False), patch(
            "agent.scheduler.run_platform_api_io",
            new=AsyncMock(return_value=payload),
        ):
            await scheduler.refresh_challenges()

        zone = scheduler.zones[Zone.Z1_SRC]
        self.assertEqual(["src-101"], [item["code"] for item in zone.challenges])
        self.assertEqual(1, zone.excluded_total)

    async def test_get_reclaimable_running_instances_ignores_partial_multi_flag_tasks(self) -> None:
        payload = {
            "current_level": 3,
            "challenges": [
                {
                    "code": "demo-solved-running",
                    "display_code": "welcome to demo solved",
                    "title": "welcome to demo solved",
                    "level": 1,
                    "difficulty": "easy",
                    "flag_count": 1,
                    "flag_got_count": 1,
                    "instance_status": "running",
                },
                {
                    "code": "z3-partial-running",
                    "display_code": "network partial",
                    "title": "network partial",
                    "level": 3,
                    "difficulty": "medium",
                    "flag_count": 3,
                    "flag_got_count": 1,
                    "instance_status": "running",
                },
            ],
        }
        scheduler = ZoneScheduler(MagicMock(), config=None)
        with patch.dict(os.environ, {"MAIN_BATTLE_DEMO_ALLOWLIST": "demo"}, clear=False), patch(
            "agent.scheduler.run_platform_api_io",
            new=AsyncMock(return_value=payload),
        ):
            await scheduler.refresh_challenges()

        reclaimable = scheduler.get_reclaimable_running_instances()
        self.assertEqual(["demo-solved-running"], [item["code"] for item in reclaimable])


class SchedulerMixedDifficultyStartTests(unittest.TestCase):
    def test_get_next_challenges_prefers_mixed_difficulty_reverse_launch_order(self) -> None:
        scheduler = ZoneScheduler(MagicMock(), config=None)
        scheduler.current_level = 1
        scheduler._update_unlock_from_level()
        scheduler.current_zone = Zone.Z1_SRC
        scheduler.zones[Zone.Z1_SRC].challenges = [
            {"code": "easy-a", "difficulty": "easy", "instance_status": "stopped", "level": 1},
            {"code": "easy-b", "difficulty": "easy", "instance_status": "stopped", "level": 1},
            {"code": "medium-a", "difficulty": "medium", "instance_status": "stopped", "level": 1},
            {"code": "medium-b", "difficulty": "medium", "instance_status": "stopped", "level": 1},
            {"code": "hard-a", "difficulty": "hard", "instance_status": "stopped", "level": 1},
            {"code": "hard-b", "difficulty": "hard", "instance_status": "stopped", "level": 1},
        ]
        scheduler._challenge_zone_index = {
            item["code"]: Zone.Z1_SRC for item in scheduler.zones[Zone.Z1_SRC].challenges
        }

        with patch("agent.scheduler.random.choice", side_effect=lambda seq: seq[-1]):
            pending = scheduler.get_next_challenges(max_count=8)

        self.assertEqual(["hard-b", "medium-b", "easy-b"], [item["code"] for item in pending[:3]])
        self.assertEqual([0, 1, 2], [item["_startup_priority"] for item in pending[:3]])

    def test_get_next_challenges_falls_back_when_not_all_three_difficulties_exist(self) -> None:
        scheduler = ZoneScheduler(MagicMock(), config=None)
        scheduler.current_level = 1
        scheduler._update_unlock_from_level()
        scheduler.current_zone = Zone.Z1_SRC
        scheduler.zones[Zone.Z1_SRC].challenges = [
            {"code": "easy-a", "difficulty": "easy", "instance_status": "stopped", "level": 1},
            {"code": "easy-b", "difficulty": "easy", "instance_status": "stopped", "level": 1},
            {"code": "hard-a", "difficulty": "hard", "instance_status": "stopped", "level": 1},
        ]
        scheduler._challenge_zone_index = {
            item["code"]: Zone.Z1_SRC for item in scheduler.zones[Zone.Z1_SRC].challenges
        }

        pending = scheduler.get_next_challenges(max_count=8)

        self.assertEqual(["easy-a", "easy-b", "hard-a"], [item["code"] for item in pending])
        self.assertTrue(all("_startup_priority" not in item for item in pending))

    def test_recently_stopped_unsolved_challenge_enters_restart_cooldown(self) -> None:
        scheduler = ZoneScheduler(MagicMock(), config=None)
        scheduler.current_level = 2
        scheduler._update_unlock_from_level()
        scheduler.current_zone = Zone.Z2_CVE
        scheduler.zones[Zone.Z2_CVE].challenges = [
            {"code": "z2-recent-stop", "difficulty": "easy", "instance_status": "stopped", "level": 2},
            {"code": "z2-fresh", "difficulty": "medium", "instance_status": "stopped", "level": 2},
        ]
        scheduler._challenge_zone_index = {
            item["code"]: Zone.Z2_CVE for item in scheduler.zones[Zone.Z2_CVE].challenges
        }

        scheduler.mark_recently_stopped_unsolved("z2-recent-stop", cooldown_seconds=180)

        pending = scheduler.get_next_challenges(max_count=8)

        self.assertEqual(["z2-fresh"], [item["code"] for item in pending])


if __name__ == "__main__":
    unittest.main()
