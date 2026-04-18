import unittest
from types import SimpleNamespace

from agent.scheduler import Zone, ZoneScheduler


class SchedulerDispatchTests(unittest.TestCase):
    def _build_scheduler(self):
        config = SimpleNamespace(
            agent=SimpleNamespace(
                max_retries=4,
                retry_backoff_seconds=60,
                attempt_history_limit=3,
            )
        )
        return ZoneScheduler(api_client=None, config=config)

    def test_get_next_challenges_does_not_trigger_mixed_start_without_three_dispatch_slots(self):
        scheduler = self._build_scheduler()
        status = scheduler.zones[Zone.Z1_SRC]
        status.unlocked = True
        status.challenges = [
            {"code": "easy-1", "difficulty": "easy", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
            {"code": "medium-1", "difficulty": "medium", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
            {"code": "hard-1", "difficulty": "hard", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
        ]
        for challenge in status.challenges:
            scheduler._challenge_zone_index[challenge["code"]] = Zone.Z1_SRC

        result = scheduler.get_next_challenges(max_count=1)

        self.assertEqual([item["code"] for item in result], ["easy-1"])

    def test_get_next_challenges_prefers_one_target_from_levels_4_3_1(self):
        scheduler = self._build_scheduler()
        for zone in (Zone.Z1_SRC, Zone.Z2_CVE, Zone.Z3_NETWORK, Zone.Z4_AD):
            scheduler.zones[zone].unlocked = True

        scheduler.zones[Zone.Z4_AD].challenges = [
            {"code": "l4-1", "difficulty": "hard", "instance_status": "stopped", "flag_count": 6, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z3_NETWORK].challenges = [
            {"code": "l3-1", "difficulty": "medium", "instance_status": "stopped", "flag_count": 2, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z2_CVE].challenges = [
            {"code": "l2-1", "difficulty": "easy", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z1_SRC].challenges = [
            {"code": "l1-1", "difficulty": "easy", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
        ]
        for zone in (Zone.Z1_SRC, Zone.Z2_CVE, Zone.Z3_NETWORK, Zone.Z4_AD):
            for challenge in scheduler.zones[zone].challenges:
                scheduler._challenge_zone_index[challenge["code"]] = zone

        result = scheduler.get_next_challenges(max_count=3)

        self.assertEqual([item["code"] for item in result], ["l4-1", "l3-1", "l1-1"])
        self.assertEqual([item.get("_startup_priority") for item in result], [0, 1, 2])

    def test_get_next_challenges_falls_back_after_levels_4_3_1(self):
        scheduler = self._build_scheduler()
        for zone in (Zone.Z1_SRC, Zone.Z2_CVE, Zone.Z3_NETWORK, Zone.Z4_AD):
            scheduler.zones[zone].unlocked = True

        scheduler.zones[Zone.Z4_AD].challenges = [
            {"code": "l4-1", "difficulty": "hard", "instance_status": "stopped", "flag_count": 6, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z3_NETWORK].challenges = [
            {"code": "l3-1", "difficulty": "medium", "instance_status": "stopped", "flag_count": 2, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z2_CVE].challenges = [
            {"code": "l2-1", "difficulty": "easy", "instance_status": "stopped", "flag_count": 1, "flag_got_count": 0},
        ]
        scheduler.zones[Zone.Z1_SRC].challenges = []
        for zone in (Zone.Z2_CVE, Zone.Z3_NETWORK, Zone.Z4_AD):
            for challenge in scheduler.zones[zone].challenges:
                scheduler._challenge_zone_index[challenge["code"]] = zone

        result = scheduler.get_next_challenges(max_count=3)

        self.assertEqual([item["code"] for item in result], ["l4-1", "l3-1", "l2-1"])


if __name__ == "__main__":
    unittest.main()
