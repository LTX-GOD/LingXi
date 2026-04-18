from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent.prompts import (
    FORUM_ADVISOR_PROMPT,
    FORUM_AGENT_PROMPT,
    MAIN_BATTLE_ADVISOR_PROMPT,
    MAIN_BATTLE_AGENT_PROMPT,
    render_prompt_template,
)


def _load_reflector_prompt(name: str) -> str:
    module = ast.parse(Path("agent/reflector.py").read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"Prompt constant not found: {name}")


REFLECTOR_PROMPT = _load_reflector_prompt("REFLECTOR_PROMPT")
SUCCESS_REFLECTION_PROMPT = _load_reflector_prompt("SUCCESS_REFLECTION_PROMPT")


class PromptRenderingTests(unittest.TestCase):
    def test_main_battle_prompt_renders_known_placeholders(self) -> None:
        rendered = render_prompt_template(
            MAIN_BATTLE_AGENT_PROMPT,
            challenge_info="challenge",
            recon_section="recon",
            skill_section="skills",
            advisor_section="advisor",
            history_section="history",
        )
        self.assertIn("execute_command", rendered)
        self.assertNotIn("{challenge_info}", rendered)

    def test_other_prompts_render_without_unexpected_placeholder_errors(self) -> None:
        prompts = (
            (
                FORUM_AGENT_PROMPT,
                {
                    "challenge_info": "challenge",
                    "recon_section": "recon",
                    "skill_section": "skills",
                    "advisor_section": "advisor",
                    "history_section": "history",
                },
            ),
            (
                MAIN_BATTLE_ADVISOR_PROMPT,
                {},
            ),
            (
                FORUM_ADVISOR_PROMPT,
                {},
            ),
            (
                REFLECTOR_PROMPT,
                {
                    "challenge_info": "challenge",
                    "action_history": "actions",
                },
            ),
            (
                SUCCESS_REFLECTION_PROMPT,
                {
                    "challenge_info": "challenge",
                    "action_summary": "summary",
                    "flag_info": "flag",
                },
            ),
        )

        for template, values in prompts:
            rendered = render_prompt_template(template, **values)
            self.assertIsInstance(rendered, str)
            self.assertNotIn("{challenge_info}", rendered)


if __name__ == "__main__":
    unittest.main()
