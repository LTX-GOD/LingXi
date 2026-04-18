from __future__ import annotations

import asyncio
import sys
import unittest
from types import ModuleType
from unittest.mock import patch


if "mcp" not in sys.modules:
    mcp_module = ModuleType("mcp")
    mcp_types_module = ModuleType("mcp.types")
    mcp_server_module = ModuleType("mcp.server")
    mcp_stdio_module = ModuleType("mcp.server.stdio")

    class _TextContent:
        def __init__(self, type: str, text: str):
            self.type = type
            self.text = text

    class _Tool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Server:
        def __init__(self, name: str):
            self.name = name

        def list_tools(self):
            def decorator(func):
                return func

            return decorator

        def call_tool(self):
            def decorator(func):
                return func

            return decorator

    mcp_types_module.TextContent = _TextContent
    mcp_types_module.Tool = _Tool
    mcp_server_module.Server = _Server
    mcp_stdio_module.stdio_server = lambda: None
    mcp_module.types = mcp_types_module
    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.types"] = mcp_types_module
    sys.modules["mcp.server"] = mcp_server_module
    sys.modules["mcp.server.stdio"] = mcp_stdio_module

import tools.mcp_server as mcp_server


class LocalMCPServerTests(unittest.TestCase):
    def test_dispatch_raises_for_unknown_tool(self) -> None:
        with self.assertRaises(mcp_server.MCPToolInvocationError) as ctx:
            asyncio.run(mcp_server._dispatch("nope", {}, challenge_code="", forum_id=0))

        self.assertIn("unknown tool", str(ctx.exception))

    def test_dispatch_raises_when_scoped_submit_tool_missing(self) -> None:
        platform_api_module = ModuleType("tools.platform_api")
        platform_api_module.get_competition_tools_for_challenge = lambda challenge_code: []
        with patch.dict(sys.modules, {"tools.platform_api": platform_api_module}):
            with self.assertRaises(mcp_server.MCPToolInvocationError) as ctx:
                asyncio.run(mcp_server._dispatch("submit_flag", {"flag": "flag{1}"}, challenge_code="web-1", forum_id=0))

        self.assertIn("submit_flag tool not available", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
