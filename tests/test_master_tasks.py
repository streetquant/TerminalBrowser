import unittest

from termdeck.master_tasks import filter_rows, row_from_record, state_from_snapshot
from termdeck.models import ResourceSnapshot, SessionRecord


class MasterTaskTests(unittest.TestCase):
    def record(self, **overrides):
        data = dict(
            id="td-abc12345",
            name="codex-braze",
            tag="bugbounty",
            command="codex",
            cwd="/work/braze",
            tmux_name="termdeck_bugbounty_codex",
            created_at="2026-07-08T00:00:00+00:00",
            last_opened_at="2026-07-08T00:00:00+00:00",
            status="running",
            last_pid=4321,
            window_id="win-1",
            window_title="Bug bounty",
            tab_index=0,
            visible=True,
        )
        data.update(overrides)
        return SessionRecord(**data)

    def test_running_snapshot_formats_browser_task_row(self):
        snap = ResourceSnapshot(
            pid=4321,
            exists=True,
            state="S",
            cpu_percent=12.345,
            memory_bytes=1536,
            runtime_seconds=65,
            process_count=3,
        )
        row = row_from_record(self.record(), pid=4321, snapshot=snap, focus_key="Alt+1", pause_key="Ctrl+Alt+1", health="OK (green)")
        self.assertEqual(row.session_id, "td-abc12345")
        self.assertEqual(row.state, "running")
        self.assertEqual(row.cpu, "12.3%")
        self.assertEqual(row.ram, "1.5 KiB")
        self.assertEqual(row.runtime, "1m 05s")
        self.assertEqual(row.health, "OK (green)")
        self.assertEqual(row.as_list()[12], "Alt+1")
        self.assertIn("bugbounty", row.search_text)
        self.assertIn("/work/braze", row.search_text)

    def test_paused_snapshot_overrides_stored_running_status(self):
        snap = ResourceSnapshot(pid=123, exists=True, state="T")
        self.assertEqual(state_from_snapshot(self.record(status="running"), snap), "paused")

    def test_missing_snapshot_keeps_explicit_paused_status(self):
        snap = ResourceSnapshot(pid=None, exists=False)
        self.assertEqual(state_from_snapshot(self.record(status="paused"), snap), "paused")
        self.assertEqual(state_from_snapshot(self.record(status="running"), snap), "stopped")
        self.assertEqual(
            state_from_snapshot(self.record(status="recovery-pending"), snap),
            "recovery-pending",
        )

    def test_filter_rows_uses_all_query_tokens(self):
        snap = ResourceSnapshot(pid=1, exists=True, state="S")
        rows = [
            row_from_record(self.record(name="codex-braze", tag="bugbounty"), pid=1, snapshot=snap, focus_key="Alt+1", pause_key="Ctrl+Alt+1"),
            row_from_record(self.record(id="td-other", name="pytest", tag="dev", command="pytest -f", cwd="/work/app", window_title="Dev"), pid=2, snapshot=snap, focus_key="Alt+2", pause_key="Ctrl+Alt+2"),
        ]
        self.assertEqual([row.name for row in filter_rows(rows, "bug bounty")], ["codex-braze"])
        self.assertEqual([row.name for row in filter_rows(rows, "bugbounty codex")], ["codex-braze"])
        self.assertEqual([row.name for row in filter_rows(rows, "pytest /work/app")], ["pytest"])


if __name__ == "__main__":
    unittest.main()
