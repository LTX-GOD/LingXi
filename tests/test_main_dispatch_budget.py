import unittest

import main


class MainDispatchBudgetTests(unittest.TestCase):
    def test_compute_main_dispatch_budget_counts_non_forum_active_and_queued(self):
        budget = main._compute_main_dispatch_budget(
            main_task_limit=5,
            scheduler_active_tasks={"main-1": object()},
            manual_active_tasks={"manual::1": object()},
            queued_codes={"main-2", "manual::2", "forum-1"},
        )

        self.assertEqual(budget, 1)

    def test_compute_main_dispatch_budget_ignores_forum_queue(self):
        budget = main._compute_main_dispatch_budget(
            main_task_limit=2,
            scheduler_active_tasks={},
            manual_active_tasks={},
            queued_codes={"forum-1", "forum-2"},
        )

        self.assertEqual(budget, 2)

    def test_challenge_level_turn_budget_overrides_l1_and_l4(self):
        self.assertEqual(main._challenge_level_turn_budget(1, 50), 300)
        self.assertEqual(main._challenge_level_turn_budget(4, 70), 1200)

    def test_challenge_level_turn_budget_keeps_existing_l2_and_l3_values(self):
        self.assertEqual(main._challenge_level_turn_budget(2, 50), 100)
        self.assertEqual(main._challenge_level_turn_budget(3, 70), 750)

    def test_challenge_level_task_timeout_only_extends_level4_main_battle(self):
        self.assertEqual(main._challenge_level_task_timeout(4, 3600), 7200)
        self.assertEqual(main._challenge_level_task_timeout(1, 3600), 3600)
        self.assertEqual(main._challenge_level_task_timeout(4, 3600, is_forum_task=True), 3600)


if __name__ == "__main__":
    unittest.main()
