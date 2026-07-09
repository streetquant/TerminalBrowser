from pathlib import Path
import tempfile
import unittest

from termdeck.models import ResourceSnapshot
from termdeck.session_reconcile import reconcile_store
from termdeck.session_store import SessionStore


class FakeBackend:
    def __init__(self, live: dict[str, int | None]):
        self.live = live

    def session_exists(self, record):
        return record.id in self.live

    def pane_pid(self, record):
        return self.live.get(record.id)


class FakeSampler:
    def __init__(self, snapshots: dict[int, ResourceSnapshot]):
        self.snapshots = snapshots

    def snapshot(self, root_pid):
        if root_pid is None:
            return ResourceSnapshot(pid=None, exists=False)
        return self.snapshots.get(root_pid, ResourceSnapshot(pid=root_pid, exists=False))


class SessionReconcileTests(unittest.TestCase):
    def test_missing_running_session_is_marked_stopped_and_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("codex", "ai", "codex", tmp)
                store.update(record.id, status="running", last_pid=123, visible=True)
                result = reconcile_store(store, backend=FakeBackend({}), sampler=FakeSampler({}))
                row = result.rows[0]
                self.assertEqual(result.changed, 1)
                self.assertEqual(row.before_status, "running")
                self.assertEqual(row.after_status, "stopped")
                self.assertFalse(row.after_visible)
                refreshed = store.get(record.id)
                self.assertEqual(refreshed.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(refreshed.visible)  # type: ignore[union-attr]
                self.assertIsNone(refreshed.last_pid)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_live_paused_process_repairs_status_without_hiding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("watch", "dev", "npm run dev", tmp)
                store.update(record.id, status="running", last_pid=None, visible=True)
                result = reconcile_store(
                    store,
                    backend=FakeBackend({record.id: 456}),
                    sampler=FakeSampler({456: ResourceSnapshot(pid=456, exists=True, state="T", process_count=1)}),
                )
                self.assertEqual(result.changed, 1)
                row = result.rows[0]
                self.assertTrue(row.live_session)
                self.assertTrue(row.live_process)
                self.assertEqual(row.after_status, "paused")
                self.assertTrue(row.after_visible)
                refreshed = store.get(record.id)
                self.assertEqual(refreshed.status, "paused")  # type: ignore[union-attr]
                self.assertEqual(refreshed.last_pid, 456)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_new_never_started_session_remains_visible_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("planned", "dev", "pytest", tmp)
                result = reconcile_store(store, backend=FakeBackend({}), sampler=FakeSampler({}))
                self.assertEqual(result.changed, 0)
                row = result.rows[0]
                self.assertEqual(row.after_status, "new")
                self.assertTrue(row.after_visible)
                self.assertEqual(row.detail, "not started yet")
            finally:
                store.close()

    def test_dry_run_reports_change_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("agent", "ai", "codex", tmp)
                store.update(record.id, status="paused", last_pid=321, visible=True)
                result = reconcile_store(store, backend=FakeBackend({}), sampler=FakeSampler({}), dry_run=True)
                self.assertEqual(result.changed, 1)
                self.assertTrue(result.rows[0].planned)
                refreshed = store.get(record.id)
                self.assertEqual(refreshed.status, "paused")  # type: ignore[union-attr]
                self.assertTrue(refreshed.visible)  # type: ignore[union-attr]
                self.assertEqual(refreshed.last_pid, 321)  # type: ignore[union-attr]
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
