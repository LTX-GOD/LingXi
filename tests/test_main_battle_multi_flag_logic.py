from __future__ import annotations

import unittest

from agent.main_battle_progress import (
    apply_main_battle_score_progress,
    compute_main_battle_solver_outcome,
    should_clear_stale_solved,
    should_mark_challenge_solved,
)


class MainBattleMultiFlagLogicTests(unittest.TestCase):
    def test_partial_main_battle_score_updates_progress_without_finishing(self) -> None:
        result = apply_main_battle_score_progress(
            content="🎉 答案正确\nFlag 进度: 1/3",
            submitted_flag="flag{alpha}",
            current_flag=None,
            scored_flags=[],
            flags_scored_count=0,
            expected_flag_count=3,
        )

        self.assertEqual(1, result["flags_scored_count"])
        self.assertEqual(3, result["expected_flag_count"])
        self.assertEqual(["flag{alpha}"], result["scored_flags"])
        self.assertTrue(result["last_submission_scored"])
        self.assertFalse(result["challenge_completed"])
        self.assertFalse(result["is_finished"])
        self.assertIn("继续寻找剩余 Flag", result["continue_message"])

    def test_completed_main_battle_score_marks_challenge_finished(self) -> None:
        result = apply_main_battle_score_progress(
            content="🎉 答案正确\nFlag 进度: 3/3",
            submitted_flag="flag{omega}",
            current_flag=None,
            scored_flags=["flag{alpha}", "flag{beta}"],
            flags_scored_count=2,
            expected_flag_count=3,
        )

        self.assertEqual(3, result["flags_scored_count"])
        self.assertEqual(3, result["expected_flag_count"])
        self.assertTrue(result["challenge_completed"])
        self.assertTrue(result["is_finished"])
        self.assertIsNone(result["continue_message"])

    def test_solver_outcome_treats_partial_new_score_as_success_but_not_completion(self) -> None:
        success, challenge_completed = compute_main_battle_solver_outcome(
            initial_flag_got_count=0,
            final_flags_scored_count=1,
            final_expected_flag_count=3,
            is_finished=False,
            explicit_challenge_completed=False,
        )

        self.assertTrue(success)
        self.assertFalse(challenge_completed)

    def test_should_mark_solved_requires_true_challenge_completion(self) -> None:
        self.assertFalse(should_mark_challenge_solved(success=True, challenge_completed=False))
        self.assertTrue(should_mark_challenge_solved(success=True, challenge_completed=True))
        self.assertFalse(should_mark_challenge_solved(success=False, challenge_completed=True))

    def test_scheduler_self_heal_clears_only_partial_running_local_solved(self) -> None:
        self.assertTrue(
            should_clear_stale_solved(
                locally_solved=True,
                flag_got_count=1,
                flag_count=3,
                instance_status="running",
            )
        )
        self.assertFalse(
            should_clear_stale_solved(
                locally_solved=True,
                flag_got_count=3,
                flag_count=3,
                instance_status="running",
            )
        )
        self.assertFalse(
            should_clear_stale_solved(
                locally_solved=True,
                flag_got_count=1,
                flag_count=3,
                instance_status="stopped",
            )
        )


if __name__ == "__main__":
    unittest.main()
