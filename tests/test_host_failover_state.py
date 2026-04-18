from __future__ import annotations

import unittest

from host_failover import HostFailoverState, is_failover_worthy_http_response


class HostFailoverStateTests(unittest.TestCase):
    def test_switches_to_fallback_after_threshold_failures(self) -> None:
        state = HostFailoverState(
            primary="https://primary.example",
            fallback="http://fallback.internal:8000",
            threshold=2,
        )

        snapshot, switched = state.record_failure("https://primary.example")
        self.assertFalse(switched)
        self.assertEqual("https://primary.example", snapshot.active)
        self.assertEqual(1, snapshot.failure_streak)

        snapshot, switched = state.record_failure("https://primary.example")
        self.assertTrue(switched)
        self.assertEqual("http://fallback.internal:8000", snapshot.active)
        self.assertEqual(2, snapshot.failure_streak)
        self.assertTrue(snapshot.switched)

    def test_primary_success_resets_streak_and_recovers_primary(self) -> None:
        state = HostFailoverState(
            primary="https://primary.example",
            fallback="http://fallback.internal:8000",
            threshold=2,
        )

        state.record_failure("https://primary.example")
        state.record_failure("https://primary.example")
        snapshot = state.record_success("https://primary.example")

        self.assertEqual("https://primary.example", snapshot.active)
        self.assertEqual(0, snapshot.failure_streak)
        self.assertFalse(snapshot.switched)

    def test_non_primary_failure_does_not_advance_failover(self) -> None:
        state = HostFailoverState(
            primary="https://primary.example",
            fallback="http://fallback.internal:8000",
            threshold=2,
        )

        snapshot, switched = state.record_failure("http://fallback.internal:8000")
        self.assertFalse(switched)
        self.assertEqual("https://primary.example", snapshot.active)
        self.assertEqual(0, snapshot.failure_streak)

    def test_failover_worthy_http_response_matches_transport_failures(self) -> None:
        self.assertTrue(is_failover_worthy_http_response(404, "application/json"))
        self.assertTrue(is_failover_worthy_http_response(500, "application/json"))
        self.assertTrue(is_failover_worthy_http_response(200, "text/html"))
        self.assertFalse(is_failover_worthy_http_response(200, "application/json"))


if __name__ == "__main__":
    unittest.main()
