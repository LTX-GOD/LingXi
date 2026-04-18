from __future__ import annotations

import unittest

from level2_task_hints import resolve_level2_task_hint


class Level2TaskHintTests(unittest.TestCase):
    def test_resolves_known_manual_task_ids(self) -> None:
        mapping = {
            "3ZdueytTkJeRy2wiYmJiqwrzP2XiNqs": ("cve-2024-1561", "gradio"),
            "FQe9I9sG0rH3oVTSYtvShoYBWhkuYEQX": ("cve-2025-67303", "comfyui-manager"),
            "p71MyGzdIAR13xvgr8SePV4UZwa6p": ("cve-2024-39907", "1panel"),
        }
        for task_id, expected in mapping.items():
            with self.subTest(task_id=task_id):
                hint = resolve_level2_task_hint(task_id, challenge_text="")
                self.assertEqual(expected[0], hint.get("cve"))
                self.assertEqual(expected[1], hint.get("poc_name"))
                self.assertEqual("2", hint.get("level"))
                self.assertEqual("z2", hint.get("zone"))

    def test_resolves_from_cve_text_without_task_id(self) -> None:
        hint = resolve_level2_task_hint(
            None,
            challenge_text="this target is likely cve-2024-39907 on 1panel",
        )
        self.assertEqual("cve-2024-39907", hint.get("cve"))
        self.assertEqual("1panel", hint.get("poc_name"))

    def test_resolves_from_known_chinese_titles(self) -> None:
        cases = {
            "算法效果展示平台": ("cve-2024-1561", "gradio"),
            "智算模型托管引擎": ("cve-2025-67303", "comfyui-manager"),
            "运维集中调度台": ("cve-2024-39907", "1panel"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                hint = resolve_level2_task_hint(None, challenge_text=title)
                self.assertEqual(expected[0], hint.get("cve"))
                self.assertEqual(expected[1], hint.get("poc_name"))


if __name__ == "__main__":
    unittest.main()
