from __future__ import annotations

import importlib
import sys
import types
import unittest


def _identity_tool(func=None, *args, **kwargs):
    if func is None:
        return lambda wrapped: wrapped
    return func


langchain_core_module = types.ModuleType("langchain_core")
langchain_core_tools_module = types.ModuleType("langchain_core.tools")
langchain_core_tools_module.tool = _identity_tool
langchain_core_module.tools = langchain_core_tools_module
sys.modules.setdefault("langchain_core", langchain_core_module)
sys.modules.setdefault("langchain_core.tools", langchain_core_tools_module)

level2_poc_module = importlib.import_module("tools.level2_cve_poc")
_build_level2_poc_command = level2_poc_module._build_level2_poc_command
level2_poc_extension_available = level2_poc_module.level2_poc_extension_available


class Level2PocToolTests(unittest.TestCase):
    def test_public_export_does_not_ship_private_poc_pack(self) -> None:
        self.assertFalse(level2_poc_extension_available())

    def test_build_command_requires_local_extension_mount(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            _build_level2_poc_command(
                "comfyui-manager",
                "https://target.example:8188",
                "check",
            )

        self.assertIn("公开仓库未附带 Level2 私有 PoC 扩展", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
