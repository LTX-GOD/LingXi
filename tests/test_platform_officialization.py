from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch


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

if "mcp" not in sys.modules:
    mcp_stub = ModuleType("mcp")
    mcp_stub.ClientSession = type("ClientSession", (), {})
    sys.modules["mcp"] = mcp_stub

    mcp_client_stub = ModuleType("mcp.client")
    sys.modules["mcp.client"] = mcp_client_stub

    mcp_stream_stub = ModuleType("mcp.client.streamable_http")
    mcp_stream_stub.streamablehttp_client = object()
    sys.modules["mcp.client.streamable_http"] = mcp_stream_stub

from tools.platform_api import (
    APIError,
    CompetitionAPIClient,
    CompetitionMCPAuthError,
    CompetitionMCPClient,
    CompetitionMCPTransportError,
    FlagRescueNotice,
    _RESCUED_FLAGS_GLOBAL,
    _SUBMITTED_FLAGS_GLOBAL,
    _submit_answer_with_instance_recovery,
    get_competition_tools,
    get_competition_tools_for_challenge,
)


def _mock_response(status_code: int, payload: dict | None = None, text: str = ""):
    response = MagicMock()
    response.status_code = status_code
    response.text = text or str(payload or {})
    if payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    response.headers = {"content-type": "application/json"}
    return response


class CompetitionAPIHostFallbackTests(unittest.TestCase):
    def test_switches_to_fallback_after_two_primary_failures(self) -> None:
        client = CompetitionAPIClient(
            "https://arena.example",
            "test-token",
            fallback_base_url="https://arena-fallback.example:8000",
        )
        client._gateway = MagicMock()
        requested_urls: list[str] = []

        def _get(url, **kwargs):
            requested_urls.append(url)
            if url.startswith("https://arena.example"):
                return _mock_response(404, {"message": "not found"})
            return _mock_response(
                200,
                {
                    "code": 0,
                    "data": {
                        "current_level": 1,
                        "total_challenges": 1,
                        "solved_challenges": 0,
                        "challenges": [{"code": "web-1"}],
                    },
                },
            )

        with patch("tools.platform_api.requests.get", side_effect=_get):
            with self.assertRaises(APIError):
                client.get_challenges()

            payload = client.get_challenges()

        self.assertEqual(1, len(payload["challenges"]))
        self.assertEqual(
            [
                "https://arena.example/api/challenges",
                "https://arena.example/api/challenges",
                "https://arena-fallback.example:8000/api/challenges",
            ],
            requested_urls,
        )
        self.assertEqual("https://arena-fallback.example:8000", client.active_base_url)

    def test_auth_error_does_not_trigger_host_fallback(self) -> None:
        client = CompetitionAPIClient(
            "https://arena.example",
            "test-token",
            fallback_base_url="https://arena-fallback.example:8000",
        )
        client._gateway = MagicMock()
        requested_urls: list[str] = []

        def _get(url, **kwargs):
            requested_urls.append(url)
            return _mock_response(401, {"message": "unauthorized"})

        with patch("tools.platform_api.requests.get", side_effect=_get):
            with self.assertRaises(APIError):
                client.get_challenges()
            with self.assertRaises(APIError):
                client.get_challenges()

        self.assertEqual(
            [
                "https://arena.example/api/challenges",
                "https://arena.example/api/challenges",
            ],
            requested_urls,
        )
        self.assertEqual("https://arena.example", client.active_base_url)


class CompetitionMCPClientTests(unittest.TestCase):
    def test_call_tool_switches_to_fallback_after_two_primary_failures(self) -> None:
        client = CompetitionMCPClient(
            "https://arena.example",
            "test-token",
            server_host_fallback="https://arena-fallback.example:8000",
        )
        requested_hosts: list[str] = []

        async def _call(host: str, tool_name: str, arguments: dict):
            requested_hosts.append(host)
            if host.startswith("https://arena.example"):
                raise CompetitionMCPTransportError("HTTP 404: not found")
            return {"code": 0, "data": {"ok": True}}

        with patch.object(client, "_ensure_config", return_value=None), patch.object(
            client,
            "_call_tool_with_host",
            side_effect=_call,
        ):
            with self.assertRaises(CompetitionMCPTransportError):
                asyncio.run(client.call_tool("list_challenges", {}))

            payload = asyncio.run(client.call_tool("list_challenges", {}))

        self.assertEqual({"code": 0, "data": {"ok": True}}, payload)
        self.assertEqual(
            [
                "https://arena.example",
                "https://arena.example",
                "https://arena-fallback.example:8000",
            ],
            requested_hosts,
        )
        self.assertEqual("https://arena-fallback.example:8000", client.active_server_host)

    def test_auth_error_does_not_trigger_mcp_host_fallback(self) -> None:
        client = CompetitionMCPClient(
            "https://arena.example",
            "test-token",
            server_host_fallback="https://arena-fallback.example:8000",
        )
        requested_hosts: list[str] = []

        async def _call(host: str, tool_name: str, arguments: dict):
            requested_hosts.append(host)
            raise CompetitionMCPAuthError("HTTP 401 unauthorized")

        with patch.object(client, "_ensure_config", return_value=None), patch.object(
            client,
            "_call_tool_with_host",
            side_effect=_call,
        ):
            with self.assertRaises(CompetitionMCPAuthError):
                asyncio.run(client.call_tool("list_challenges", {}))
            with self.assertRaises(CompetitionMCPAuthError):
                asyncio.run(client.call_tool("list_challenges", {}))

        self.assertEqual(
            [
                "https://arena.example",
                "https://arena.example",
            ],
            requested_hosts,
        )
        self.assertEqual("https://arena.example", client.active_server_host)


class CompetitionToolAliasTests(unittest.TestCase):
    def test_global_tools_include_official_names_and_legacy_aliases(self) -> None:
        tool_names = {tool.name for tool in get_competition_tools()}
        self.assertTrue(
            {
                "list_challenges",
                "start_challenge",
                "stop_challenge",
                "submit_flag",
            }.issubset(tool_names)
        )
        self.assertTrue(
            {
                "get_challenge_list",
                "start_challenge_instance",
                "stop_challenge_instance",
            }.issubset(tool_names)
        )

    def test_scoped_tools_only_include_submit_flag(self) -> None:
        tool_names = {tool.name for tool in get_competition_tools_for_challenge("web-1")}
        self.assertIn("submit_flag", tool_names)
        self.assertEqual({"submit_flag"}, tool_names)


class FlagRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        _SUBMITTED_FLAGS_GLOBAL.clear()
        _RESCUED_FLAGS_GLOBAL.clear()

    def test_submit_answer_with_instance_recovery_retries_after_restart(self) -> None:
        client = MagicMock()
        client.submit_answer.side_effect = [
            APIError("HTTP 400: 赛题实例未运行"),
            {
                "correct": True,
                "message": "答案正确",
                "flag_count": 1,
                "flag_got_count": 1,
            },
        ]
        client.start_challenge.return_value = {"data": ["target.example:80"]}

        with patch("tools.platform_api.time.sleep"):
            data, note = _submit_answer_with_instance_recovery(
                client,
                "web-1",
                "flag{demo}",
            )

        self.assertTrue(data["correct"])
        self.assertIn("补提成功", note)
        client.start_challenge.assert_called_once_with("web-1")

    def test_submit_answer_with_instance_recovery_caches_flag_when_restart_fails(self) -> None:
        client = MagicMock()
        client.submit_answer.side_effect = APIError("HTTP 400: 赛题实例未运行")
        client.start_challenge.side_effect = APIError("HTTP 400: 答题开关切换中")

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"LINGXI_FLAG_RESCUE_PATH": str(Path(temp_dir) / "rescued.txt")},
            clear=False,
        ):
            with self.assertRaises(FlagRescueNotice) as ctx:
                _submit_answer_with_instance_recovery(client, "web-2", "flag{demo}")

            content = (Path(temp_dir) / "rescued.txt").read_text(encoding="utf-8")

        self.assertIn("flag{demo}", content)
        self.assertIn("尚未得分", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
