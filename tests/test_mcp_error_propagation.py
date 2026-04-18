from __future__ import annotations

import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


if "langchain_core.tools" not in sys.modules:
    langchain_core_module = ModuleType("langchain_core")
    langchain_tools_module = ModuleType("langchain_core.tools")

    class _StructuredTool:
        @staticmethod
        def from_function(*, coroutine=None, **kwargs):
            return SimpleNamespace(coroutine=coroutine, **kwargs)

    def _tool(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    langchain_tools_module.StructuredTool = _StructuredTool
    langchain_tools_module.tool = _tool
    sys.modules["langchain_core"] = langchain_core_module
    sys.modules["langchain_core.tools"] = langchain_tools_module

if "pydantic" not in sys.modules:
    pydantic_stub = ModuleType("pydantic")
    pydantic_stub.BaseModel = object
    pydantic_stub.Field = lambda *args, **kwargs: None
    pydantic_stub.create_model = lambda name, **kwargs: type(name, (), {})
    sys.modules["pydantic"] = pydantic_stub

import tools.forum_api as forum_api
import tools.kali_mcp as kali_mcp
import tools.sliver_mcp as sliver_mcp


class MCPErrorPropagationTests(unittest.TestCase):
    def test_sliver_wrapper_raises_after_retry_failure(self) -> None:
        spec = SimpleNamespace(name="list_sessions", description="desc", inputSchema={})
        tool = sliver_mcp._build_mcp_tool(spec)

        with patch.object(
            sliver_mcp,
            "get_sliver_mcp_runner",
            return_value=SimpleNamespace(call_tool=lambda name, kwargs: (_ for _ in ()).throw(sliver_mcp.SliverMCPError("boom"))),
        ), patch.object(
            sliver_mcp,
            "reconnect_sliver_mcp",
            side_effect=RuntimeError("retry failed"),
        ):
            with self.assertRaises(sliver_mcp.SliverMCPError) as ctx:
                asyncio.run(tool.coroutine())

        self.assertIn("自动重连后仍失败", str(ctx.exception))

    def test_forum_wrapper_raises_after_retry_failure(self) -> None:
        spec = SimpleNamespace(name="get_posts", description="desc", inputSchema={})
        tool = forum_api._build_mcp_tool(spec)

        with patch.object(
            forum_api,
            "get_forum_mcp_runner",
            return_value=SimpleNamespace(call_tool=lambda name, kwargs: (_ for _ in ()).throw(forum_api.ForumMCPError("boom"))),
        ), patch.object(
            forum_api,
            "reconnect_forum_services",
            side_effect=RuntimeError("retry failed"),
        ):
            with self.assertRaises(forum_api.ForumMCPError) as ctx:
                asyncio.run(tool.coroutine())

        self.assertIn("自动重连后仍失败", str(ctx.exception))

    def test_kali_wrapper_raises_after_retry_failure(self) -> None:
        spec = SimpleNamespace(name="run_cmd", description="desc", inputSchema={})
        tool = kali_mcp._build_kali_tool(spec, "kali-research")

        with patch.object(
            kali_mcp,
            "get_kali_mcp_runner",
            return_value=SimpleNamespace(
                container="kali-research",
                port=5001,
                call_tool=lambda name, kwargs: (_ for _ in ()).throw(kali_mcp.KaliMCPError("boom")),
            ),
        ), patch.object(
            kali_mcp,
            "initialize_kali_mcp",
            side_effect=RuntimeError("retry failed"),
        ):
            with self.assertRaises(kali_mcp.KaliMCPError) as ctx:
                asyncio.run(tool.coroutine())

        self.assertIn("重连后仍失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
