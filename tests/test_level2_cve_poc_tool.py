from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


if "langchain_core.tools" not in sys.modules:
    langchain_core_stub = ModuleType("langchain_core")
    langchain_tools_stub = ModuleType("langchain_core.tools")
    langchain_tools_stub.tool = lambda *args, **kwargs: (lambda fn: fn)
    langchain_core_stub.tools = langchain_tools_stub
    sys.modules["langchain_core"] = langchain_core_stub
    sys.modules["langchain_core.tools"] = langchain_tools_stub

from tools.level2_cve_poc import _parse_gradio_hunt_paths, _run_level2_poc_impl


class Level2CvePocToolTests(unittest.TestCase):
    def test_missing_extension_returns_public_export_message(self) -> None:
        output = _run_level2_poc_impl("gradio", "https://target.example:7860", "check", "")
        self.assertIn("公开仓库未附带 Level2 私有 PoC 扩展", output)

    def test_parse_gradio_hunt_paths_uses_defaults_and_deduplicates(self) -> None:
        defaults = _parse_gradio_hunt_paths("")
        self.assertIn("/flag", defaults)
        self.assertIn("/app/flag", defaults)

        custom = _parse_gradio_hunt_paths("/flag,\napp/flag,/flag")
        self.assertEqual(["/flag", "/app/flag"], custom)

    def test_gradio_hunt_flag_tries_multiple_paths_until_flag_found(self) -> None:
        fake_results = [
            SimpleNamespace(exit_code=0, stdout="hostname\n", stderr=""),
            SimpleNamespace(exit_code=0, stdout="flag{demo-success}\n", stderr=""),
        ]
        with patch("tools.level2_cve_poc.level2_poc_extension_available", return_value=True), patch(
            "tools.level2_cve_poc._script_path_for",
            return_value=Path("/tmp/fake-gradio-poc"),
        ), patch.object(Path, "exists", autospec=True, return_value=True), patch(
            "tools.level2_cve_poc.validate_network_target",
            return_value="",
        ), patch(
            "tools.level2_cve_poc.validate_execution_text",
            return_value="",
        ), patch("tools.level2_cve_poc._execute", side_effect=fake_results) as mocked_execute:
            output = _run_level2_poc_impl("gradio", "https://target.example:7860", "hunt_flag", "")

        self.assertEqual(2, mocked_execute.call_count)
        self.assertIn("--file /flag", mocked_execute.call_args_list[0].args[0])
        self.assertIn("--file /flag.txt", mocked_execute.call_args_list[1].args[0])
        self.assertIn("Attempt: 1/", output)
        self.assertIn("File Candidate: /flag.txt", output)
        self.assertIn("flag{demo-success}", output)


if __name__ == "__main__":
    unittest.main()
