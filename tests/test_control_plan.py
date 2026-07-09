import unittest

from termdeck.control_plan import build_control_plan, destructive_hints, preview_text, render_control_plan_text
from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.session_store import utc_now


def record(name="codex-one", tag="ai", status="running"):
    now = utc_now()
    return SessionRecord(
        id="td-12345678",
        name=name,
        tag=tag,
        command="codex",
        cwd="/tmp",
        tmux_name=f"termdeck_{tag}_{name}",
        created_at=now,
        last_opened_at=now,
        status=status,
        last_pid=123,
    )


class ControlPlanTests(unittest.TestCase):
    def test_destructive_hints_detect_common_terminal_footguns(self):
        hints = destructive_hints("git reset --hard && rm -rf build")
        self.assertIn("hard reset", hints)
        self.assertIn("recursive delete", hints)

    def test_preview_text_is_single_line_and_bounded(self):
        text = preview_text("one\n two\tthree " + "x" * 200, limit=20)
        self.assertEqual(len(text), 20)
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith("…"))

    def test_multi_send_enter_requires_confirmation(self):
        rows = [record("codex-one"), record("codex-two")]
        plan = build_control_plan(rows, "send", text="status", enter=True, tag="ai")
        self.assertEqual(plan.risk, "high")
        self.assertTrue(plan.requires_confirmation)
        self.assertEqual(plan.count, 2)
        self.assertEqual(plan.text_preview, "status")

    def test_send_without_enter_is_low_risk(self):
        plan = build_control_plan([record()], "send", text="status", enter=False)
        self.assertEqual(plan.risk, "low")
        self.assertFalse(plan.requires_confirmation)

    def test_destructive_send_is_critical_even_for_one_session(self):
        plan = build_control_plan([record()], "send", text="kubectl delete pod prod", enter=True)
        self.assertEqual(plan.risk, "critical")
        self.assertTrue(plan.requires_confirmation)
        self.assertIn("kubernetes delete", plan.destructive_hints)

    def test_kill_requires_confirmation(self):
        plan = build_control_plan([record()], "kill")
        self.assertEqual(plan.risk, "high")
        self.assertTrue(plan.requires_confirmation)
        self.assertIn("stops", plan.rows[0].reason)


    def test_render_control_plan_text_is_gui_ready_and_bounded(self):
        rows = [record(f"codex-{idx}") for idx in range(10)]
        plan = build_control_plan(rows, "send", text="rm -rf build", enter=True, tag="ai")
        rendered = render_control_plan_text(plan, row_limit=3)
        self.assertIn("Action: send", rendered)
        self.assertIn("Risk: critical", rendered)
        self.assertIn("Destructive-looking input: recursive delete", rendered)
        self.assertIn("td-12345678 [ai] codex-0", rendered)
        self.assertIn("…and 7 more session(s)", rendered)

    def test_health_badge_is_included_when_snapshot_exists(self):
        snap = ResourceSnapshot(pid=123, exists=True, cpu_percent=0.0, memory_bytes=10_000, runtime_seconds=2, process_count=1)
        plan = build_control_plan([record()], "pause", pid_resolver=lambda _r: 123, snapshot_resolver=lambda _p: snap)
        self.assertIn("OK", plan.rows[0].health)


if __name__ == "__main__":
    unittest.main()
