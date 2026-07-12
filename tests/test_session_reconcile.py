from pathlib import Path
import tempfile
import unittest

from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.session_reconcile import reconcile_records, reconcile_store
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


class BatchBackend(FakeBackend):
    def __init__(self, live: dict[str, int | None]):
        super().__init__(live)
        self.exists_many_calls = 0
        self.pane_pids_calls = 0

    def session_exists(self, record):
        raise AssertionError("batch reconciliation should not query sessions individually")

    def pane_pid(self, record):
        raise AssertionError("batch reconciliation should not query pane pids individually")

    def session_exists_many(self, records):
        self.exists_many_calls += 1
        return {record.id: record.id in self.live for record in records}

    def pane_pids(self, records):
        self.pane_pids_calls += 1
        return {record.id: self.live.get(record.id) for record in records}


class BudgetSampler:
    def __init__(self):
        self.calls = []

    def snapshots(self, root_pids, *, refresh_limit=None):
        self.calls.append((len(root_pids), refresh_limit))
        return [
            ResourceSnapshot(pid=pid, exists=pid is not None, state="unknown" if pid else "missing")
            for pid in root_pids
        ]

    def snapshot(self, _root_pid):
        raise AssertionError("fleet reconciliation should use the batch sampler")


class BulkStore:
    def __init__(self):
        self.calls = []

    def update_runtime_states(self, updates):
        self.calls.append(list(updates))

    def update(self, *_args, **_kwargs):
        raise AssertionError("fleet reconciliation should use one bulk database update")


class StalePaneBackend(BatchBackend):
    pane_snapshot_stale = True


class SessionReconcileTests(unittest.TestCase):
    def test_stale_pane_snapshot_never_turns_sessions_missing(self):
        running = SessionRecord(
            id="running",
            name="running",
            tag="ai",
            command="codex",
            cwd="/tmp",
            tmux_name="running",
            created_at="now",
            last_opened_at="now",
            status="running",
            last_pid=101,
        )
        paused = SessionRecord(
            id="paused",
            name="paused",
            tag="ai",
            command="claude",
            cwd="/tmp",
            tmux_name="paused",
            created_at="now",
            last_opened_at="now",
            status="paused",
            last_pid=202,
        )
        backend = StalePaneBackend({"running": 101, "paused": 202})
        sampler = BudgetSampler()

        result = reconcile_records([running, paused], backend=backend, sampler=sampler)

        self.assertEqual(sampler.calls, [])
        self.assertEqual([row.after_status for row in result.rows], ["running", "paused"])
        self.assertTrue(all(row.after_visible for row in result.rows))

    def test_two_thousand_sessions_use_bounded_sampling_and_one_store_write(self):
        records = [
            SessionRecord(
                id=f"td-{index}",
                name=f"agent-{index}",
                tag="ai",
                command="codex",
                cwd="/tmp",
                tmux_name=f"agent-{index}",
                created_at="now",
                last_opened_at="now",
                status="new",
            )
            for index in range(2000)
        ]
        backend = BatchBackend({record.id: 20_000 + index for index, record in enumerate(records)})
        sampler = BudgetSampler()
        store = BulkStore()

        result = reconcile_records(
            records,
            store=store,
            backend=backend,
            sampler=sampler,
            resource_sample_limit=128,
        )

        self.assertEqual(result.count, 2000)
        self.assertEqual(result.changed, 2000)
        self.assertEqual(sampler.calls, [(2000, 128)])
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(len(store.calls[0]), 2000)
        self.assertEqual(backend.exists_many_calls, 1)
        self.assertEqual(backend.pane_pids_calls, 1)

    def test_many_sessions_use_two_backend_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "dev", "bash", tmp)
                second = store.create("second", "dev", "bash", tmp)
                backend = BatchBackend({first.id: 101, second.id: 202})
                sampler = FakeSampler(
                    {
                        101: ResourceSnapshot(pid=101, exists=True, state="S"),
                        202: ResourceSnapshot(pid=202, exists=True, state="S"),
                    }
                )

                result = reconcile_store(store, backend=backend, sampler=sampler)

                self.assertEqual(result.count, 2)
                self.assertEqual(backend.exists_many_calls, 1)
                self.assertEqual(backend.pane_pids_calls, 1)
            finally:
                store.close()
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
