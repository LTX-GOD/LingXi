from __future__ import annotations

import unittest

from agent.prompts import (
    FORUM_ADVISOR_PROMPT,
    KALI_DOCKER_CONTAINER_NAME,
    MAIN_BATTLE_ADVISOR_PROMPT,
    MAIN_BATTLE_AGENT_PROMPT,
)


class PromptStrategyTests(unittest.TestCase):
    def test_main_battle_prompt_keeps_core_execution_contract(self) -> None:
        for keyword in (
            "execute_command",
            "execute_python",
            "run_level2_cve_poc",
            "submit_flag",
            "flag{...}",
            "反注入安全",
            "收割多枚 Flag",
            "当前题目 `- 目标:`",
            "默认 `/flag`",
            KALI_DOCKER_CONTAINER_NAME,
        ):
            self.assertIn(keyword, MAIN_BATTLE_AGENT_PROMPT)

    def test_main_battle_advisor_prompt_uses_temporary_three_part_context(self) -> None:
        for keyword in (
            "临时顾问",
            "只做一次性纠偏",
            "当前题目描述",
            "最新一条主攻手决策",
            "最近一条工具返回",
            "当前最可能的卡点是什么",
            "下一步最低成本、最高信号的验证动作是什么",
            "现在不要继续做什么",
            "只输出一段短指令",
            "最多 120 字",
        ):
            self.assertIn(keyword, MAIN_BATTLE_ADVISOR_PROMPT)

    def test_forum_advisor_prompt_uses_temporary_three_part_context(self) -> None:
        for keyword in (
            "临时顾问",
            "当前论坛模块描述",
            "最新一条主攻手决策",
            "最近一条工具返回",
            "forum-2",
            "forum-4",
            "只输出一段短指令",
            "最多 120 字",
        ):
            self.assertIn(keyword, FORUM_ADVISOR_PROMPT)


if __name__ == "__main__":
    unittest.main()
