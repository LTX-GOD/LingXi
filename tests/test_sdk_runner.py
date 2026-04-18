import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from claude_code_sdk.types import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent import sdk_runner


class SDKRunnerEnvTests(unittest.TestCase):
    def test_resolve_advisor_timeout_seconds_defaults_to_100(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sdk_runner._resolve_advisor_timeout_seconds(), 100.0)

    def test_build_main_sdk_env_uses_main_anthropic_gateway(self):
        with patch.object(sdk_runner, "_TOOL_PROXY_BIN_DIR", "/tmp/does-not-exist"):
            with patch.object(sdk_runner, "_ensure_sdk_runtime_env_loaded", return_value=None):
                with patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "MAIN_LLM_PROVIDER": "anthropic",
                        "SDK_MODEL": "claude-opus-main",
                        "ANTHROPIC_BASE_URL": "http://main-gateway",
                        "ANTHROPIC_API_KEY": "main-demo-key",
                        "ANTHROPIC_MODEL": "claude-opus-4-6",
                    },
                    clear=True,
                ):
                    env = sdk_runner._build_main_sdk_env()

        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://main-gateway")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "main-demo-key")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-main")

    def test_build_advisor_sdk_env_maps_deepseek_to_anthropic_vars(self):
        with patch.object(sdk_runner, "_TOOL_PROXY_BIN_DIR", "/tmp/does-not-exist"):
            with patch.object(sdk_runner, "_ensure_sdk_runtime_env_loaded", return_value=None):
                with patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "ADVISOR_LLM_PROVIDER": "deepseek",
                        "SDK_ADVISOR_MODEL": "deepseek-advisor",
                        "DEEPSEEK_BASE_URL": "http://advisor-gateway",
                        "DEEPSEEK_API_KEY": "advisor-demo-key",
                        "DEEPSEEK_MODEL": "deepseek-ai/DeepSeek-R1",
                    },
                    clear=True,
                ):
                    env = sdk_runner._build_advisor_sdk_env()

        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://advisor-gateway")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "advisor-demo-key")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-advisor")

    def test_resolve_sdk_session_concurrency_prefers_explicit_env(self):
        with patch.dict(
            os.environ,
            {
                "LINGXI_SDK_MAX_CONCURRENCY": "1",
                "COMPETITION_GATEWAY_LLM_MAX_CONCURRENCY": "4",
            },
            clear=True,
        ):
            self.assertEqual(sdk_runner._resolve_sdk_session_concurrency(), 1)

    def test_resolve_sdk_session_concurrency_defaults_to_three(self):
        with patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            self.assertEqual(sdk_runner._resolve_sdk_session_concurrency(), 3)

    def test_build_advisor_reasons_includes_periodic_consultation(self):
        reasons = sdk_runner._build_advisor_reasons(
            no_tool_rounds=0,
            advisor_no_tool_threshold=2,
            consecutive_failures=0,
            consecutive_failures_threshold=3,
            advisor_consultation_interval=5,
            total_turns=5,
            last_periodic_advisor_turn=0,
        )

        self.assertEqual(reasons, ["periodic_consultation(turn=5,current=5,interval=5)"])

    def test_build_advisor_reasons_skips_duplicate_periodic_turn(self):
        reasons = sdk_runner._build_advisor_reasons(
            no_tool_rounds=0,
            advisor_no_tool_threshold=2,
            consecutive_failures=0,
            consecutive_failures_threshold=3,
            advisor_consultation_interval=5,
            total_turns=5,
            last_periodic_advisor_turn=5,
        )

        self.assertEqual(reasons, [])

    def test_build_advisor_reasons_triggers_after_crossing_period_boundary(self):
        reasons = sdk_runner._build_advisor_reasons(
            no_tool_rounds=0,
            advisor_no_tool_threshold=2,
            consecutive_failures=0,
            consecutive_failures_threshold=3,
            advisor_consultation_interval=5,
            total_turns=6,
            last_periodic_advisor_turn=0,
        )

        self.assertEqual(reasons, ["periodic_consultation(turn=5,current=6,interval=5)"])

    def test_resolve_response_turn_budget_uses_smallest_positive_threshold(self):
        budget = sdk_runner._resolve_response_turn_budget(
            max_turns=120,
            advisor_no_tool_threshold=2,
            consecutive_failures_threshold=3,
            advisor_consultation_interval=5,
        )

        self.assertEqual(budget, 2)

    def test_extract_langchain_text_content_handles_string_and_blocks(self):
        self.assertEqual(sdk_runner._extract_langchain_text_content(" hello "), "hello")
        self.assertEqual(
            sdk_runner._extract_langchain_text_content([{"text": "one"}, {"content": "two"}]),
            "one\ntwo",
        )

    def test_build_advisor_followup_prompt_contains_hard_requirements(self):
        prompt = sdk_runner._build_advisor_followup_prompt("先手动测试 /login 和 /add_prescription")
        self.assertIn("强制执行顾问指令", prompt)
        self.assertIn("执行顾问建议:", prompt)
        self.assertIn("先手动测试 /login 和 /add_prescription", prompt)

    def test_should_enforce_advisor_directive_skips_fallback_message(self):
        self.assertFalse(sdk_runner._should_enforce_advisor_directive(sdk_runner._ADVISOR_FALLBACK_MESSAGE))
        self.assertTrue(sdk_runner._should_enforce_advisor_directive("先手动测试 /login"))


class SDKRunnerCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_await_sdk_close_times_out_in_same_task(self):
        async def sleeper():
            await asyncio.sleep(0.05)

        status = await sdk_runner._await_sdk_close(
            sleeper(),
            label="client",
            challenge_code="demo",
            timeout=0.01,
        )

        self.assertEqual(status, "timeout")

    async def test_await_sdk_close_ignores_known_close_scope_errors(self):
        async def fail():
            raise RuntimeError("Attempted to exit cancel scope in a different task than it was entered in")

        status = await sdk_runner._await_sdk_close(
            fail(),
            label="client",
            challenge_code="demo",
            timeout=1.0,
            ignore_error_markers=sdk_runner._SDK_CLOSE_SCOPE_MARKERS,
        )

        self.assertEqual(status, "ignored")

    async def test_can_use_tool_blocks_actionable_tool_until_advisor_acknowledged(self):
        state = sdk_runner.RunnerState(
            challenge={"code": "demo", "flag_count": 1, "flag_got_count": 0},
            is_forum=False,
            is_testenv=False,
        )
        state.advisor_directive_pending = "先手动测试 /login"
        can_use_tool = sdk_runner._make_can_use_tool(state)

        denied = await can_use_tool("execute_python", {"code": "print('x')"}, None)
        self.assertIsInstance(denied, PermissionResultDeny)
        self.assertIn("执行顾问建议", denied.message)

        state.current_strategy = "执行顾问建议: 先手动测试 /login"
        state.decision_history.append(state.current_strategy)
        allowed = await can_use_tool("execute_python", {"code": "print('x')"}, None)
        self.assertIsInstance(allowed, PermissionResultAllow)
        self.assertEqual(state.advisor_directive_pending, "")

    async def test_can_use_tool_blocks_repeated_msf_commands(self):
        state = sdk_runner.RunnerState(
            challenge={"code": "demo", "flag_count": 1, "flag_got_count": 0},
            is_forum=False,
            is_testenv=False,
        )
        can_use_tool = sdk_runner._make_can_use_tool(state)

        first = await can_use_tool("execute_command", {"command": "msfconsole -q -x 'help'"}, None)
        self.assertIsInstance(first, PermissionResultAllow)

        second = await can_use_tool("execute_command", {"command": "msfconsole -q -x 'search smb'"}, None)
        self.assertIsInstance(second, PermissionResultDeny)
        self.assertIn("Metasploit", second.message)


class SDKRunnerStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_system_message_failure_reason_detects_api_retry(self):
        message = SystemMessage(
            subtype="api_retry",
            data={"type": "system", "subtype": "api_retry", "error_status": 500, "error": "server_error"},
        )

        self.assertEqual(
            sdk_runner._system_message_failure_reason(message),
            "api_retry:500:server_error",
        )

    def test_system_message_failure_reason_ignores_init_messages(self):
        message = SystemMessage(
            subtype="init",
            data={"type": "system", "subtype": "init", "tools": ["Bash", "Edit"]},
        )

        self.assertEqual(sdk_runner._system_message_failure_reason(message), "")

    async def test_system_message_does_not_count_as_meaningful_first_response(self):
        async def stream():
            yield SystemMessage(subtype="notice", data={"type": "system", "subtype": "notice"})
            await asyncio.sleep(0.05)

        with self.assertRaises(TimeoutError) as ctx:
            async for _ in sdk_runner._iterate_with_timeouts(
                stream(),
                first_timeout=0.01,
                idle_timeout=0.5,
                counts_as_progress=sdk_runner._message_counts_as_progress,
            ):
                pass

        self.assertIn("first_response_timeout", str(ctx.exception))

    async def test_run_agent_periodic_advisor_triggers_with_chunked_responses(self):
        class FakeClient:
            def __init__(self, options):
                self.options = options
                self.prompts = []
                self.response_index = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def query(self, prompt):
                self.prompts.append(prompt)

            async def receive_response(self):
                self.response_index += 1
                if self.response_index == 1:
                    yield AssistantMessage(
                        content=[
                            TextBlock(text="probe"),
                            ToolUseBlock(id="tool-1", name="execute_command", input={"command": "curl http://target"}),
                        ],
                        model="test-model",
                    )
                    yield UserMessage(
                        content=[
                            ToolResultBlock(tool_use_id="tool-1", content="ok", is_error=False),
                        ]
                    )
                    yield ResultMessage(
                        subtype="success",
                        duration_ms=1,
                        duration_api_ms=1,
                        is_error=False,
                        num_turns=5,
                        session_id="sess-1",
                        total_cost_usd=0.0,
                    )
                    return
                yield UserMessage(
                    content=[
                        ToolResultBlock(tool_use_id="tool-2", content="🎉 恭喜！答案正确（1/1），获得0分\nFlag 进度: 1/1", is_error=False),
                    ]
                )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="sess-1",
                    total_cost_usd=0.0,
                )

        progress = {}
        async def fake_advisor(state, advisor_model, latest_decision, latest_tool_result, *, reason=""):
            state.advisor_call_count += 1
            state.advisor_history.append(f"advisor reason={reason}")
            sdk_runner._sync_progress_snapshot(state)
            return "改用下一步动作"

        advisor_mock = AsyncMock(side_effect=fake_advisor)
        with patch.object(sdk_runner, "ClaudeSDKClient", FakeClient):
            with patch.object(sdk_runner, "_call_advisor", advisor_mock):
                result = await sdk_runner.run_agent(
                    challenge={"code": "demo", "flag_count": 1, "flag_got_count": 0},
                    system_prompt="sys",
                    initial_prompt="start",
                    advisor_model="advisor-model",
                    max_turns=20,
                    advisor_no_tool_threshold=99,
                    advisor_consultation_interval=5,
                    consecutive_failures_threshold=99,
                    progress_snapshot=progress,
                )

        self.assertEqual(advisor_mock.await_count, 1)
        self.assertEqual(result["advisor_call_count"], 1)
        self.assertTrue(result["is_finished"])
        self.assertEqual(progress["advisor_call_count"], 1)

    async def test_run_agent_startup_retry_waits_before_retrying(self):
        class FakeClient:
            instance_count = 0

            def __init__(self, options):
                self.options = options
                self.prompts = []
                FakeClient.instance_count += 1
                self.instance_id = FakeClient.instance_count

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def query(self, prompt):
                self.prompts.append(prompt)

            async def receive_response(self):
                if self.instance_id == 1:
                    yield SystemMessage(
                        subtype="api_retry",
                        data={
                            "type": "system",
                            "subtype": "api_retry",
                            "error_status": 502,
                            "error": "bad gateway",
                        },
                    )
                    return
                yield AssistantMessage(
                    content=[
                        TextBlock(text="retry ok"),
                        ToolUseBlock(id="tool-1", name="execute_command", input={"command": "id"}),
                    ],
                    model="test-model",
                )
                yield UserMessage(
                    content=[
                        ToolResultBlock(tool_use_id="tool-1", content="🎉 恭喜！答案正确（1/1），获得0分\nFlag 进度: 1/1", is_error=False),
                    ]
                )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="sess-1",
                    total_cost_usd=0.0,
                )

        sleep_mock = AsyncMock()
        with patch.object(sdk_runner, "ClaudeSDKClient", FakeClient):
            with patch.object(sdk_runner, "_SDK_STARTUP_RETRY_ATTEMPTS", 2):
                with patch.object(sdk_runner, "_sdk_startup_retry_backoff_seconds", return_value=3.0) as backoff_mock:
                    with patch.object(sdk_runner.asyncio, "sleep", sleep_mock):
                        result = await sdk_runner.run_agent(
                            challenge={"code": "demo", "flag_count": 1, "flag_got_count": 0},
                            system_prompt="sys",
                            initial_prompt="start",
                            max_turns=20,
                            advisor_no_tool_threshold=99,
                            advisor_consultation_interval=0,
                            consecutive_failures_threshold=99,
                        )

        backoff_mock.assert_called_once_with(1)
        sleep_mock.assert_awaited_once_with(3.0)
        self.assertEqual(FakeClient.instance_count, 2)
        self.assertTrue(result["is_finished"])

    async def test_run_agent_advisor_fallback_does_not_leave_pending_directive(self):
        captured_client = {}

        class FakeClient:
            def __init__(self, options):
                self.options = options
                self.prompts = []
                self.response_index = 0
                captured_client["client"] = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def query(self, prompt):
                self.prompts.append(prompt)

            async def receive_response(self):
                self.response_index += 1
                if self.response_index == 1:
                    yield AssistantMessage(
                        content=[
                            TextBlock(text="probe"),
                            ToolUseBlock(id="tool-1", name="execute_command", input={"command": "curl http://target"}),
                        ],
                        model="test-model",
                    )
                    yield UserMessage(
                        content=[
                            ToolResultBlock(tool_use_id="tool-1", content="ok", is_error=False),
                        ]
                    )
                    yield ResultMessage(
                        subtype="success",
                        duration_ms=1,
                        duration_api_ms=1,
                        is_error=False,
                        num_turns=5,
                        session_id="sess-1",
                        total_cost_usd=0.0,
                    )
                    return
                yield AssistantMessage(
                    content=[
                        TextBlock(text="continue probing"),
                        ToolUseBlock(id="tool-2", name="execute_command", input={"command": "curl http://target/login"}),
                    ],
                    model="test-model",
                )
                yield UserMessage(
                    content=[
                        ToolResultBlock(tool_use_id="tool-2", content="🎉 恭喜！答案正确（1/1），获得0分\nFlag 进度: 1/1", is_error=False),
                    ]
                )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="sess-1",
                    total_cost_usd=0.0,
                )

        progress = {}
        advisor_mock = AsyncMock(return_value=sdk_runner._ADVISOR_FALLBACK_MESSAGE)
        with patch.object(sdk_runner, "ClaudeSDKClient", FakeClient):
            with patch.object(sdk_runner, "_call_advisor", advisor_mock):
                result = await sdk_runner.run_agent(
                    challenge={"code": "demo", "flag_count": 1, "flag_got_count": 0},
                    system_prompt="sys",
                    initial_prompt="start",
                    advisor_model="advisor-model",
                    max_turns=20,
                    advisor_no_tool_threshold=99,
                    advisor_consultation_interval=5,
                    consecutive_failures_threshold=99,
                    progress_snapshot=progress,
                )

        self.assertEqual(advisor_mock.await_count, 1)
        self.assertEqual(progress["advisor_directive_pending"], "")
        self.assertIn("顾问本轮不可用", captured_client["client"].prompts[-1])
        self.assertNotIn("强制执行顾问指令", captured_client["client"].prompts[-1])
        self.assertTrue(result["is_finished"])


if __name__ == "__main__":
    unittest.main()
