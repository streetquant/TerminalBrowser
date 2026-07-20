import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from termdeck.activity import (
    activity_to_dicts,
    classify_activity,
    filter_activities,
    render_activity_table,
    scan_activities,
    sort_activities,
)
from termdeck.session_store import SessionStore


class FakeBackend:
    def __init__(self, captures):
        self.captures = captures

    def capture(self, record, lines=80):
        value = self.captures.get(record.name, "")
        if isinstance(value, Exception):
            raise value
        return value


class ActivityTests(unittest.TestCase):
    def _record(self, name="codex", command="codex"):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                return store.create(name, "ai", command, tmp, window_id="agents", window_title="Agents", tab_index=1)
            finally:
                store.close()

    def test_classifies_waiting_for_confirmation_before_generic_output(self):
        record = self._record()
        item = classify_activity(record, "Tool wants to continue? [y/N]\n")
        self.assertEqual(item.state, "needs input")
        self.assertEqual(item.severity, 4)
        self.assertIn("confirmation", item.reason)

    def test_classifies_error_warning_success_and_silent_output(self):
        record = self._record("tests", "pytest")
        self.assertEqual(classify_activity(record, "Traceback (most recent call last):\nValueError\n").state, "error")
        self.assertEqual(classify_activity(record, "WARNING deprecated option\n").state, "warning")
        self.assertIn(classify_activity(record, "12 tests passed in 1.2s\n").state, {"success", "ready"})
        self.assertEqual(classify_activity(record, "\n\n").state, "not started")

    def test_empty_recovery_pending_capture_surfaces_exact_restore_blocker(self):
        record = replace(
            self._record("worker", "python worker.py"),
            status="recovery-pending",
        )
        item = classify_activity(record, "\n")
        self.assertEqual(item.state, "recovery pending")
        self.assertEqual(item.level, "red")
        self.assertEqual(item.suggested_action, "inspect checkpoint status")

    def test_redacts_secret_like_capture_in_preview(self):
        record = self._record("api", "python server.py")
        item = classify_activity(record, "OPENAI_API_KEY=sk-proj-1234567890abcdefghijk\nserver ready\n")
        self.assertTrue(item.redacted)
        self.assertIn("[REDACTED", item.preview)
        self.assertNotIn("sk-proj-1234567890abcdefghijk", item.preview)

    def test_scan_filter_sort_and_table_are_browser_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                waiting = store.create("codex-one", "ai", "codex", tmp, window_title="Agents", tab_index=1)
                server = store.create("web-server", "dev", "npm run dev", tmp, window_title="Development", tab_index=0)
                failed = store.create("pytest", "tests", "pytest -q", tmp, window_title="Development", tab_index=2)
            finally:
                store.close()

            backend = FakeBackend({
                "codex-one": "Approve command? (yes/no)\n",
                "web-server": "listening on http://localhost:3000\n",
                "pytest": "FAILED tests/test_app.py::test_demo\n",
            })
            items = scan_activities([server, waiting, failed], backend=backend)
            self.assertEqual({item.record.name: item.state for item in items}["codex-one"], "needs input")
            filtered = filter_activities(items, "agents input")
            self.assertEqual([item.record.name for item in filtered], ["codex-one"])
            sorted_items = sort_activities(items)
            self.assertEqual(sorted_items[0].severity, 4)
            rows = activity_to_dicts(sorted_items)
            self.assertIn("suggested_action", rows[0])
            table = render_activity_table(sorted_items)
            self.assertIn("Activity", table)
            self.assertIn("codex-one", table)

    def test_backend_capture_failure_becomes_unavailable(self):
        record = self._record("missing", "ssh host")
        items = scan_activities([record], backend=FakeBackend({"missing": RuntimeError("tmux unavailable")}))
        self.assertEqual(items[0].state, "unavailable")
        self.assertEqual(items[0].suggested_action, "open or restore session")


if __name__ == "__main__":
    unittest.main()
