from __future__ import annotations

import unittest

from main_battle_task_hints import (
    format_main_battle_task_hint,
    resolve_main_battle_task_hint,
)


class MainBattleTaskHintsTests(unittest.TestCase):
    def test_resolve_layer_breach_hint_by_code(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "code": "K7kbx40FbhQNODZkS",
                "display_code": "Layer Breach",
            }
        )

        self.assertEqual("Layer Breach", hint.get("display"))
        self.assertIn("不起眼的文件", str(hint.get("official_hint", "")))
        self.assertEqual("data/artifacts/Layer_Breach", hint.get("artifact_dir"))

    def test_resolve_layer_breach_hint_by_title_alias(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "title": "layer_breach",
                "description": "multi-layer network penetration",
            }
        )

        self.assertIn("dump 到本地", str(hint.get("official_hint", "")))
        self.assertEqual("data/artifacts/Layer_Breach", hint.get("artifact_dir"))

    def test_unrelated_challenge_description_does_not_trigger_layer_breach_hint(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "code": "other-code",
                "title": "Different Challenge",
                "description": "notes mention Layer Breach as a prior writeup reference",
            }
        )

        self.assertEqual({}, hint)

    def test_resolve_level4_langflow_hint_by_level(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "code": "level4-code",
                "title": "Final Assault",
                "level": 4,
            }
        )

        self.assertEqual("Langflow 1.2.0", hint.get("service"))
        self.assertIn("6 个 Flag", str(hint.get("flag_goal", "")))
        self.assertIn("impacket-smbclient", " ".join(hint.get("tooling", ())))
        self.assertIn("未开源", " ".join(hint.get("instructions", ())))
        self.assertIn("ADCS", " ".join(hint.get("instructions", ())))

    def test_resolve_level4_langflow_hint_by_description_keyword(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "code": "other-code",
                "description": "External service is Langflow 1.2.0 with container escape path",
            }
        )

        self.assertEqual("Langflow 1.2.0", hint.get("service"))
        self.assertIn("公开仓库不附带", " ".join(hint.get("instructions", ())))

    def test_resolve_pydash_hint_by_title(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "title": "PyDash",
            }
        )

        self.assertEqual("PyDash", hint.get("display"))
        self.assertIn("/src", " ".join(hint.get("instructions", ())))
        self.assertIn("Unicode", " ".join(hint.get("instructions", ())))
        self.assertIn("session", " ".join(hint.get("instructions", ())))

    def test_resolve_cloudfunc_hint_by_description_keyword(self) -> None:
        hint = resolve_main_battle_task_hint(
            {
                "description": "CloudFunc admin API with JWT verification issue",
            }
        )

        self.assertEqual("CloudFunc", hint.get("display"))
        self.assertIn("kid", " ".join(hint.get("instructions", ())))
        self.assertIn("john", " ".join(hint.get("instructions", ())))

    def test_format_layer_breach_hint_contains_artifact_guidance(self) -> None:
        rendered = format_main_battle_task_hint(
            resolve_main_battle_task_hint(
                {
                    "code": "K7kbx40FbhQNODZkS",
                    "display_code": "Layer Breach",
                }
            )
        )

        self.assertIn("官方提示", rendered)
        self.assertIn("data/artifacts/Layer_Breach", rendered)
        self.assertIn("strings", rendered)
        self.assertIn("binwalk", rendered)

    def test_format_level4_hint_contains_service_poc_and_escape_guidance(self) -> None:
        rendered = format_main_battle_task_hint(
            resolve_main_battle_task_hint(
                {
                    "level": 4,
                    "title": "Langflow Mayhem",
                }
            )
        )

        self.assertIn("Langflow 1.2.0", rendered)
        self.assertIn("6 个 Flag", rendered)
        self.assertIn("impacket-smbclient", rendered)
        self.assertIn("公开仓库不附带", rendered)
        self.assertIn("ADCS", rendered)
        self.assertIn("impacket-wmiexec -h", rendered)


if __name__ == "__main__":
    unittest.main()
