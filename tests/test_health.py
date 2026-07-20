from __future__ import annotations

import unittest

from termdeck.health import HealthPolicy, evaluate_health, should_pause_for_budget
from termdeck.models import ResourceSnapshot, SessionRecord


def record(status: str = "running") -> SessionRecord:
    return SessionRecord(
        id="td-test",
        name="test",
        tag="default",
        command="sleep 10",
        cwd="/tmp",
        tmux_name="termdeck_test",
        created_at="2026-01-01T00:00:00+00:00",
        last_opened_at="2026-01-01T00:00:00+00:00",
        status=status,
    )


class HealthTests(unittest.TestCase):
    def test_running_task_inside_budget_is_ok(self) -> None:
        health = evaluate_health(
            record(),
            ResourceSnapshot(pid=123, exists=True, state="S", cpu_percent=12.0, memory_bytes=64 * 1024 * 1024, process_count=2),
        )
        self.assertEqual(health.label, "OK")
        self.assertFalse(health.is_attention)

    def test_paused_state_is_reported(self) -> None:
        health = evaluate_health(record(), ResourceSnapshot(pid=123, exists=True, state="T"))
        self.assertEqual(health.label, "paused")
        self.assertEqual(health.severity, 1)

    def test_missing_running_session_needs_attention(self) -> None:
        health = evaluate_health(record(), ResourceSnapshot(pid=None, exists=False))
        self.assertEqual(health.label, "missing")
        self.assertTrue(health.is_attention)

    def test_recovery_pending_is_not_presented_as_a_restartable_missing_task(self) -> None:
        health = evaluate_health(
            record(status="recovery-pending"),
            ResourceSnapshot(pid=None, exists=False),
        )
        self.assertEqual(health.label, "recovery pending")
        self.assertEqual(health.suggested_action, "inspect checkpoint status")
        self.assertTrue(health.is_attention)

    def test_critical_memory_wins_over_high_cpu(self) -> None:
        policy = HealthPolicy(high_cpu_percent=100.0, critical_cpu_percent=300.0, high_memory_bytes=1024, critical_memory_bytes=2048)
        health = evaluate_health(
            record(),
            ResourceSnapshot(pid=123, exists=True, state="R", cpu_percent=150.0, memory_bytes=4096, process_count=4),
            policy,
        )
        self.assertEqual(health.label, "critical memory")
        self.assertIn("high CPU", health.hint)
        self.assertTrue(should_pause_for_budget(health))

    def test_large_process_tree_is_attention_even_when_cpu_and_memory_are_low(self) -> None:
        health = evaluate_health(
            record(),
            ResourceSnapshot(pid=123, exists=True, state="S", cpu_percent=0.5, memory_bytes=1024, process_count=64),
        )
        self.assertEqual(health.label, "many processes")
        self.assertTrue(health.is_attention)


if __name__ == "__main__":
    unittest.main()
