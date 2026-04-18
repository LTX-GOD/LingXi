from __future__ import annotations

import os
import sys
import unittest
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

    langchain_tools_stub.StructuredTool = type("StructuredTool", (), {})
    langchain_tools_stub.tool = _tool
    langchain_core_stub.tools = langchain_tools_stub
    sys.modules["langchain_core"] = langchain_core_stub
    sys.modules["langchain_core.tools"] = langchain_tools_stub

from config import ForumConfig
from tools.forum_api import ForumAPIClient, ForumAPIError, initialize_forum_mcp


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


class ForumConfigFallbackTests(unittest.TestCase):
    def test_forum_config_reads_optional_fallback_host(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SERVER_HOST": "https://forum.example",
                "SERVER_HOST_FALLBACK": "https://forum-fallback.example",
            },
            clear=False,
        ):
            forum = ForumConfig()

        self.assertEqual("https://forum.example", forum.server_host)
        self.assertEqual("https://forum-fallback.example", forum.server_host_fallback)


class ForumAPIHostFallbackTests(unittest.TestCase):
    def test_switches_to_fallback_after_two_primary_failures(self) -> None:
        client = ForumAPIClient(
            "https://forum.example",
            "test-token",
            "https://forum-fallback.example",
        )
        client._gateway = MagicMock()
        requested_urls: list[str] = []

        def _request(method, url, **kwargs):
            requested_urls.append(url)
            if url.startswith("https://forum.example"):
                return _mock_response(404, {"message": "not found"})
            return _mock_response(200, {"code": 0, "data": [{"id": 1, "title": "forum"}]})

        with patch("tools.forum_api.requests.request", side_effect=_request):
            with self.assertRaises(ForumAPIError):
                client.get_challenges()

            payload = client.get_challenges()

        self.assertEqual([{"id": 1, "title": "forum"}], payload)
        self.assertEqual(
            [
                "https://forum.example/api/v1/agent/flags/challenges",
                "https://forum.example/api/v1/agent/flags/challenges",
                "https://forum-fallback.example/api/v1/agent/flags/challenges",
            ],
            requested_urls,
        )
        self.assertEqual("https://forum-fallback.example", client.active_server_host)

    def test_auth_error_does_not_trigger_host_fallback(self) -> None:
        client = ForumAPIClient(
            "https://forum.example",
            "test-token",
            "https://forum-fallback.example",
        )
        client._gateway = MagicMock()
        requested_urls: list[str] = []

        def _request(method, url, **kwargs):
            requested_urls.append(url)
            return _mock_response(401, {"message": "unauthorized"})

        with patch("tools.forum_api.requests.request", side_effect=_request):
            with self.assertRaises(ForumAPIError):
                client.get_challenges()
            with self.assertRaises(ForumAPIError):
                client.get_challenges()

        self.assertEqual(
            [
                "https://forum.example/api/v1/agent/flags/challenges",
                "https://forum.example/api/v1/agent/flags/challenges",
            ],
            requested_urls,
        )

    def test_initialize_forum_mcp_forwards_fallback_host(self) -> None:
        with patch("tools.forum_api.ForumMCPRunner") as runner_cls:
            runner = runner_cls.return_value
            result = initialize_forum_mcp(
                "https://forum.example",
                "test-token",
                "https://forum-fallback.example",
            )

        self.assertIs(result, runner)
        runner.start.assert_called_once()
        runner_cls.assert_called_once()
        _, kwargs = runner_cls.call_args
        self.assertEqual("https://forum.example", kwargs["server_host"])
        self.assertEqual("https://forum-fallback.example", kwargs["server_host_fallback"])


if __name__ == "__main__":
    unittest.main()
