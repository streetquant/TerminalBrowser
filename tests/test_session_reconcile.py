from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace

from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.session_reconcile import (
    reconcile_records,
    reconcile_store,
    startup_recovery_allowed,
    startup_restore_candidates,
)
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
    def test_startup_candidates_require_active_state_and_valid_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                active = store.create("active", "dev", "bash", tmp)
                paused = store.create("paused", "dev", "bash", tmp)
                missing = store.create("missing", "dev", "bash", tmp)
                pending = store.create("pending", "dev", "bash", tmp)
                start_error = store.create("start-error", "dev", "bash", tmp)
                never = store.create("never", "dev", "bash", tmp)
                remembered = store.create("remembered", "dev", "bash", tmp)
                stopped = store.create("stopped", "dev", "bash", tmp)
                archived = store.create("archived", "dev", "bash", tmp)
                external = store.create("external", "dev", "bash", tmp)
                manual = store.create("manual", "dev", "bash", tmp, restart_policy="manual")
                corrupt = store.create("corrupt", "dev", "bash", tmp)
                for record, status in (
                    (active, "running"),
                    (paused, "paused"),
                    (missing, "missing"),
                    (pending, "recovery-pending"),
                    (start_error, "start-error"),
                    (never, "new"),
                    (remembered, "remembered"),
                    (stopped, "stopped"),
                    (archived, "archived"),
                    (external, "running"),
                    (manual, "running"),
                ):
                    store.update(record.id, status=status, visible=True)
                store.update(external.id, external=True)
                corrupt = replace(
                    corrupt,
                    status="recovery-pending",
                    recovery_recipe="{}",
                    recovery_recipe_sha256="0" * 64,
                )
                records = [
                    corrupt if record.id == corrupt.id else record
                    for record in store.list(include_archived=True)
                ]

                selected = startup_restore_candidates(records)

                self.assertEqual(
                    {record.id for record in selected},
                    {active.id, paused.id, missing.id, pending.id},
                )
                self.assertFalse(startup_recovery_allowed(start_error))
                self.assertFalse(startup_recovery_allowed(never))
                self.assertFalse(startup_recovery_allowed(remembered))
                self.assertFalse(startup_recovery_allowed(stopped))
                self.assertFalse(startup_recovery_allowed(external))
                self.assertFalse(startup_recovery_allowed(manual))
                self.assertFalse(startup_recovery_allowed(corrupt))
            finally:
                store.close()

    def test_durable_intent_overrides_ambiguous_start_error_and_running_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                start_error = store.create("start-error", "dev", "bash", tmp)
                running = store.create("running", "dev", "bash", tmp)
                start_error = store.update(start_error.id, status="start-error")
                running = store.update(running.id, status="running")
                active_retry = SimpleNamespace(
                    **{**start_error.__dict__, "recovery_intent": "active"}
                )
                explicit_stop = SimpleNamespace(
                    **{**running.__dict__, "recovery_intent": "stopped"}
                )

                self.assertTrue(startup_recovery_allowed(active_retry))
                self.assertFalse(startup_recovery_allowed(explicit_stop))
            finally:
                store.close()

    def test_live_manual_tab_is_reattached_but_missing_manual_tab_is_not_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                manual_live = store.create("manual-live", "dev", "bash", tmp, restart_policy="manual")
                manual_missing = store.create("manual-missing", "dev", "bash", tmp, restart_policy="manual")
                manual_live = store.update(manual_live.id, status="running")
                manual_missing = store.update(manual_missing.id, status="running")

                selected = startup_restore_candidates(
                    [manual_live, manual_missing],
                    live_session_ids=[manual_live.id],
                )

                self.assertEqual([record.id for record in selected], [manual_live.id])
            finally:
                store.close()

    def test_explicit_stop_wins_over_stale_live_backend_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                stopped = store.create("stopped", "dev", "bash", tmp)
                store.update(stopped.id, status="stopped", last_pid=None, visible=True)
                result = reconcile_store(
                    store,
                    backend=FakeBackend({stopped.id: 321}),
                    sampler=FakeSampler({321: ResourceSnapshot(pid=321, exists=True, state="S")}),
                    recover_missing=True,
                )

                self.assertEqual(result.rows[0].after_status, "stopped")
                self.assertEqual(result.rows[0].detail, "explicitly stopped record preserved")
                self.assertIsNone(store.get(stopped.id).last_pid)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_never_started_rows_are_preserved_during_startup_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                remembered = store.create("remembered", "dev", "bash", tmp)
                store.update(remembered.id, status="remembered", visible=True)
                result = reconcile_store(
                    store,
                    backend=FakeBackend({remembered.id: 123}),
                    sampler=FakeSampler({123: ResourceSnapshot(pid=123, exists=True, state="S")}),
                    recover_missing=True,
                )

                self.assertEqual(result.rows[0].after_status, "remembered")
                self.assertEqual(result.changed, 0)
            finally:
                store.close()

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

    def test_startup_recovery_preserves_every_internal_tab_for_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                visible = store.create(
                    "codex",
                    "ai",
                    "codex --yolo",
                    tmp,
                    window_id="agents",
                    window_title="Agents",
                    tab_index=3,
                )
                hidden = store.create(
                    "shell",
                    "dev",
                    "bash",
                    tmp,
                    window_id="agents",
                    window_title="Agents",
                    tab_index=4,
                    visible=False,
                )
                stopped = store.create("stopped", "dev", "bash", tmp)
                external = store.create("adopted", "dev", "bash", tmp)
                store.update(visible.id, status="running", last_pid=101)
                store.update(hidden.id, status="paused", last_pid=202)
                store.update(stopped.id, status="stopped", visible=False, last_pid=None)
                store.update(external.id, status="running", external=True, last_pid=303)

                result = reconcile_store(
                    store,
                    backend=FakeBackend({}),
                    sampler=FakeSampler({}),
                    recover_missing=True,
                )

                recovered_visible = store.get(visible.id)
                recovered_hidden = store.get(hidden.id)
                preserved_stopped = store.get(stopped.id)
                unavailable_external = store.get(external.id)
                self.assertEqual(recovered_visible.status, "recovery-pending")  # type: ignore[union-attr]
                self.assertTrue(recovered_visible.visible)  # type: ignore[union-attr]
                self.assertIsNone(recovered_visible.last_pid)  # type: ignore[union-attr]
                self.assertEqual(recovered_visible.window_id, "agents")  # type: ignore[union-attr]
                self.assertEqual(recovered_visible.window_title, "Agents")  # type: ignore[union-attr]
                self.assertEqual(recovered_visible.tab_index, 3)  # type: ignore[union-attr]
                self.assertEqual(recovered_visible.command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(recovered_visible.cwd, tmp)  # type: ignore[union-attr]
                self.assertEqual(recovered_hidden.status, "recovery-pending")  # type: ignore[union-attr]
                self.assertFalse(recovered_hidden.visible)  # type: ignore[union-attr]
                self.assertEqual(preserved_stopped.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(preserved_stopped.visible)  # type: ignore[union-attr]
                self.assertEqual(unavailable_external.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(unavailable_external.visible)  # type: ignore[union-attr]
                self.assertEqual(result.changed, 3)
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
