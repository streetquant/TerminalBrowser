import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck.recovery_coordinator import RecoveryCoordinator
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend


class FakeBackend:
    def __init__(self):
        self.live = {}
        self.create_calls = []
        self.ready = threading.Event()

    def session_exists(self, record):
        return record.id in self.live

    def authoritative_live_pane_pid(self, record):
        return self.live.get(record.id)

    def pane_pid(self, record):
        return self.live.get(record.id)

    def create_or_resume(self, record):
        self.create_calls.append(record.id)
        self.ready.wait(1)
        self.live[record.id] = 7001


class ProbedBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.recovery_state = {"state": "pending"}

    def recovery_readiness(self, _record):
        return self.recovery_state


class PayloadBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.recovery_calls = []

    def recover_dead_payload(self, record):
        self.recovery_calls.append(record.id)
        self.live[record.id] = 8201
        return SimpleNamespace(
            action="shell",
            pid=8201,
            command="/usr/bin/bash -i",
            cwd=record.cwd,
            clear_recovery=True,
        )


class RecoveryCoordinatorTests(unittest.TestCase):
    def _store_record(self, store, *, name="session", command="bash", **changes):
        record = store.create(name, "dev", command, str(Path(store.path).parent))
        changes.setdefault("status", "running")
        return store.update(record.id, visible=True, **changes)

    def test_invalid_or_non_positive_timeout_uses_safe_default(self):
        for configured in ("", "not-a-number", "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(configured=configured):
                with mock.patch.dict(
                    os.environ,
                    {"TERMDECK_RECOVERY_READINESS_TIMEOUT": configured},
                    clear=False,
                ):
                    coordinator = RecoveryCoordinator(FakeBackend(), None)
                try:
                    self.assertEqual(
                        coordinator.readiness_timeout,
                        RecoveryCoordinator.DEFAULT_READINESS_TIMEOUT,
                    )
                finally:
                    coordinator.shutdown(wait=True)

        coordinator = RecoveryCoordinator(FakeBackend(), None, readiness_timeout=0)
        try:
            self.assertEqual(
                coordinator.readiness_timeout,
                RecoveryCoordinator.DEFAULT_READINESS_TIMEOUT,
            )
        finally:
            coordinator.shutdown(wait=True)

    def test_backend_readiness_probe_distinguishes_ready_pending_and_failed(self):
        backend = TmuxBackend(executable="tmux")
        record = type("Record", (), {"command": "codex --yolo"})()
        with mock.patch.object(backend, "authoritative_live_pane_pid", return_value=8123):
            with mock.patch(
                "termdeck.tmux_backend.process_tree_contains_codex",
                return_value=True,
            ):
                self.assertEqual(
                    backend.recovery_readiness(record),
                    {"state": "ready", "pid": 8123},
                )

            with mock.patch(
                "termdeck.tmux_backend.process_tree_contains_codex",
                return_value=False,
            ), mock.patch.object(backend, "_process_tree_is_idle_shell", return_value=True):
                result = backend.recovery_readiness(record)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["pid"], 8123)

            with mock.patch(
                "termdeck.tmux_backend.process_tree_contains_codex",
                return_value=False,
            ), mock.patch.object(backend, "_process_tree_is_idle_shell", return_value=False):
                self.assertEqual(
                    backend.recovery_readiness(record),
                    {"state": "pending", "pid": 8123},
                )

            with mock.patch(
                "termdeck.tmux_backend.process_tree_contains_codex",
                return_value=None,
            ):
                self.assertEqual(
                    backend.recovery_readiness(record),
                    {"state": "pending", "pid": 8123},
                )

    def test_explicit_backend_failure_does_not_wait_or_create_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = ProbedBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                readiness_timeout=30,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(
                    store,
                    name="placeholder",
                    command="codex --yolo",
                )
                backend.live[record.id] = 8124
                backend.recovery_state = {
                    "state": "failed",
                    "pid": 8124,
                    "detail": "launcher exited to fallback shell",
                }
                results = []
                coordinator.submit(record, on_failed=results.append)
                deadline = time.monotonic() + 1
                while not results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].state, "failed")
                self.assertIn("fallback shell", results[0].error)
                self.assertEqual(backend.create_calls, [])
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_single_flight_updates_durable_status_and_waits_for_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store)
                results = []
                first = coordinator.submit(record, on_ready=results.append)
                second = coordinator.submit(record, on_ready=results.append)
                self.assertEqual(first.state, "queued")
                self.assertEqual(second.state, "deduplicated")
                self.assertEqual(store.get(record.id).status, "recovery-pending")
                time.sleep(0.03)
                self.assertEqual(backend.create_calls, [record.id])
                self.assertEqual(results, [])
                backend.ready.set()
                deadline = time.monotonic() + 2
                while len(results) != 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(results), 2)
                self.assertTrue(all(result.ready for result in results))
                self.assertEqual(store.get(record.id).status, "running")
                self.assertEqual(store.get(record.id).last_pid, 7001)
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_live_target_is_delivered_without_queueing_or_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(backend, store)
            try:
                record = self._store_record(store)
                backend.live[record.id] = 7002
                results = []
                submission = coordinator.submit(
                    record,
                    known_live=True,
                    known_pid=7002,
                    on_ready=results.append,
                )
                self.assertEqual(submission.state, "live")
                self.assertEqual(results[0].pid, 7002)
                self.assertEqual(backend.create_calls, [])
                self.assertEqual(coordinator.inflight_ids, frozenset())
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_manual_stopped_never_and_external_rows_are_not_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(backend, store)
            try:
                rows = [
                    self._store_record(store, name="manual", restart_policy="manual"),
                    self._store_record(store, name="stopped", status="stopped"),
                    self._store_record(store, name="never", status="new"),
                    self._store_record(store, name="external", external=True),
                ]
                for row in rows:
                    submission = coordinator.submit(row)
                    self.assertFalse(submission.accepted)
                    self.assertEqual(submission.state, "rejected")
                self.assertEqual(backend.create_calls, [])
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_known_live_inactive_rows_are_not_marked_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(backend, store)
            try:
                rows = [
                    self._store_record(
                        store,
                        name="manual-live",
                        restart_policy="manual",
                    ),
                    self._store_record(store, name="stopped-live", status="stopped"),
                    self._store_record(store, name="archived-live", status="archived"),
                    self._store_record(store, name="external-live", external=True),
                ]
                ready = []
                for index, row in enumerate(rows, start=7200):
                    before = store.get(row.id)
                    backend.live[row.id] = index
                    submission = coordinator.submit(
                        row,
                        known_live=True,
                        known_pid=index,
                        on_ready=ready.append,
                    )
                    self.assertFalse(submission.accepted)
                    self.assertEqual(submission.state, "rejected")
                    current = store.get(row.id)
                    self.assertEqual(current.status, before.status)
                    self.assertEqual(current.last_pid, before.last_pid)
                    self.assertEqual(current.recovery_intent, before.recovery_intent)
                self.assertEqual(ready, [])
                self.assertEqual(backend.create_calls, [])
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_reopened_inactive_intent_beats_stale_live_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(path)
            backend = FakeBackend()
            record = self._store_record(store, name="persisted-inactive")
            stale = record
            store.mark_recovery_inactive(record.id)
            store.close()
            reopened = SessionStore(path)
            coordinator = RecoveryCoordinator(backend, reopened)
            try:
                backend.live[record.id] = 7201
                ready = []
                submission = coordinator.submit(
                    stale,
                    known_live=True,
                    known_pid=7201,
                    on_ready=ready.append,
                )
                self.assertFalse(submission.accepted)
                self.assertEqual(submission.state, "rejected")
                self.assertEqual(ready, [])
                current = reopened.get(record.id)
                self.assertEqual(current.recovery_intent, "inactive")
                self.assertNotEqual(current.status, "recovery-pending")
            finally:
                coordinator.shutdown(wait=True)
                reopened.close()

    def test_concurrent_stop_before_worker_reread_prevents_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(path)
            backend = FakeBackend()
            factory_entered = threading.Event()
            release_factory = threading.Event()

            def store_factory():
                factory_entered.set()
                release_factory.wait(1)
                return SessionStore(path)

            coordinator = RecoveryCoordinator(
                backend,
                store,
                store_factory=store_factory,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store, name="stop-before-launch")
                failures = []
                coordinator.submit(record, on_failed=failures.append)
                self.assertTrue(factory_entered.wait(1))
                store.update(record.id, status="stopped")
                release_factory.set()
                deadline = time.monotonic() + 1
                while not failures and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].state, "skipped")
                self.assertEqual(backend.create_calls, [])
                current = store.get(record.id)
                self.assertEqual(current.status, "stopped")
                self.assertEqual(current.recovery_intent, "inactive")
            finally:
                release_factory.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_stop_after_launch_cannot_be_overwritten_by_ready_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store, name="stop-during-launch")
                failures = []
                coordinator.submit(record, on_failed=failures.append)
                deadline = time.monotonic() + 1
                while not backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(backend.create_calls, [record.id])
                store.update(record.id, status="archived")
                backend.ready.set()
                deadline = time.monotonic() + 1
                while not failures and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].state, "skipped")
                current = store.get(record.id)
                self.assertEqual(current.status, "archived")
                self.assertEqual(current.recovery_intent, "inactive")
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_worker_store_failure_reports_callback_without_fanout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()

            def failing_factory():
                raise OSError("worker database unavailable")

            coordinator = RecoveryCoordinator(
                backend,
                store,
                store_factory=failing_factory,
            )
            try:
                record = self._store_record(store, name="worker-store-failure")
                failures = []
                coordinator.submit(record, on_failed=failures.append)
                deadline = time.monotonic() + 1
                while not failures and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].state, "failed")
                self.assertIn("worker database unavailable", failures[0].error)
                self.assertEqual(backend.create_calls, [])
                self.assertEqual(store.get(record.id).status, "recovery-pending")
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_shutdown_notifies_listeners_for_queued_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = self._store_record(store, name="shutdown-first")
                second = self._store_record(store, name="shutdown-second")
                first_results = []
                second_results = []
                coordinator.submit(first, on_ready=first_results.append, on_failed=first_results.append)
                deadline = time.monotonic() + 1
                while not backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(backend.create_calls, [first.id])
                coordinator.submit(
                    second,
                    on_ready=second_results.append,
                    on_failed=second_results.append,
                )
                coordinator.shutdown(wait=False)
                deadline = time.monotonic() + 1
                while not second_results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(second_results), 1)
                self.assertEqual(second_results[0].state, "failed")
                self.assertIn("shut down", second_results[0].error)
                backend.ready.set()
                deadline = time.monotonic() + 1
                while not first_results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(first_results[0].state, "ready")
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_recovery_does_not_mark_running_before_backend_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                readiness_timeout=0.05,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store)
                results = []
                coordinator.submit(record, on_ready=results.append)
                deadline = time.monotonic() + 1
                while not backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(store.get(record.id).status, "recovery-pending")
                backend.ready.set()
                deadline = time.monotonic() + 1
                while not results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(results[0].pid, 7001)
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_codex_waits_for_descendant_readiness_before_next_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = self._store_record(store, name="first", command="codex --yolo")
                second = self._store_record(store, name="second", command="codex --yolo")
                results = []
                codex_ready = False

                def descendant_ready(_pid):
                    return codex_ready

                with mock.patch(
                    "termdeck.recovery_coordinator.process_tree_contains_codex",
                    side_effect=descendant_ready,
                ):
                    coordinator.submit(first, on_ready=results.append)
                    coordinator.submit(second, on_ready=results.append)
                    backend.ready.set()
                    deadline = time.monotonic() + 1
                    while len(backend.create_calls) < 1 and time.monotonic() < deadline:
                        time.sleep(0.005)
                    time.sleep(0.03)
                    self.assertEqual(backend.create_calls, [first.id])
                    self.assertEqual(results, [])
                    codex_ready = True
                    deadline = time.monotonic() + 1
                    while len(results) < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                self.assertEqual(backend.create_calls, [first.id, second.id])
                self.assertEqual(len(results), 2)
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_known_live_codex_pane_is_queued_until_descendant_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = self._store_record(store, name="first", command="codex --yolo")
                second = self._store_record(store, name="second", command="codex --yolo")
                backend.live[first.id] = 7101
                results = []
                codex_ready = False

                def descendant_ready(_pid):
                    return codex_ready

                with mock.patch(
                    "termdeck.recovery_coordinator.process_tree_contains_codex",
                    side_effect=descendant_ready,
                ):
                    first_submission = coordinator.submit(
                        first,
                        known_live=True,
                        known_pid=7101,
                        on_ready=results.append,
                    )
                    coordinator.submit(second, on_ready=results.append)
                    self.assertEqual(first_submission.state, "queued")
                    time.sleep(0.03)
                    self.assertEqual(results, [])
                    self.assertEqual(backend.create_calls, [])
                    codex_ready = True
                    backend.ready.set()
                    deadline = time.monotonic() + 1
                    while len(results) < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                self.assertEqual(len(results), 2)
                self.assertEqual(backend.create_calls, [second.id])
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_existing_codex_target_probe_waits_for_descendant_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store, command="codex --yolo")
                backend.live[record.id] = 7102
                results = []
                codex_ready = False

                with mock.patch(
                    "termdeck.recovery_coordinator.process_tree_contains_codex",
                    side_effect=lambda _pid: codex_ready,
                ):
                    coordinator.submit(record, on_ready=results.append)
                    time.sleep(0.03)
                    self.assertEqual(results, [])
                    self.assertEqual(backend.create_calls, [])
                    codex_ready = True
                    deadline = time.monotonic() + 1
                    while not results and time.monotonic() < deadline:
                        time.sleep(0.005)
                self.assertEqual(results[0].pid, 7102)
                self.assertEqual(backend.create_calls, [])
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_pending_codex_over_ten_seconds_keeps_next_launch_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            backend.ready.set()
            now = 0.0
            past_ten = threading.Event()
            release = threading.Event()
            codex_ready = False

            def clock():
                return now

            def sleeper(_interval):
                nonlocal now
                now += 1.0
                if now > 10:
                    past_ten.set()
                    release.wait()

            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=30,
                poll_interval=1,
                clock=clock,
                sleeper=sleeper,
            )
            try:
                first = self._store_record(store, name="first", command="codex --yolo")
                second = self._store_record(store, name="second", command="codex --yolo")
                results = []

                with mock.patch(
                    "termdeck.recovery_coordinator.process_tree_contains_codex",
                    side_effect=lambda _pid: codex_ready,
                ):
                    coordinator.submit(first, on_ready=results.append)
                    coordinator.submit(second, on_ready=results.append)
                    self.assertTrue(past_ten.wait(1))
                    self.assertEqual(backend.create_calls, [first.id])
                    self.assertEqual(results, [])
                    codex_ready = True
                    release.set()
                    deadline = time.monotonic() + 1
                    while len(results) < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                self.assertEqual(backend.create_calls, [first.id, second.id])
                self.assertEqual(len(results), 2)
            finally:
                release.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_recovery_pool_defaults_to_four_and_is_safely_clamped(self):
        cases = {
            "": RecoveryCoordinator.DEFAULT_MAX_WORKERS,
            "not-a-number": RecoveryCoordinator.DEFAULT_MAX_WORKERS,
            "0": RecoveryCoordinator.MIN_MAX_WORKERS,
            "-2": RecoveryCoordinator.MIN_MAX_WORKERS,
            "2": 2,
            "16": 16,
            "99": RecoveryCoordinator.MAX_MAX_WORKERS,
        }
        for configured, expected in cases.items():
            with self.subTest(configured=configured):
                with mock.patch.dict(
                    os.environ,
                    {RecoveryCoordinator.WORKER_COUNT_ENV: configured},
                    clear=True,
                ):
                    coordinator = RecoveryCoordinator(FakeBackend(), None)
                try:
                    self.assertEqual(coordinator.max_workers, expected)
                    self.assertEqual(coordinator._executor._max_workers, expected)
                finally:
                    coordinator.shutdown(wait=True)

    def test_independent_targets_use_parallel_recovery_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=2,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = self._store_record(store, name="independent-a")
                second = self._store_record(store, name="independent-b")
                results = []
                coordinator.submit(first, on_ready=results.append)
                coordinator.submit(second, on_ready=results.append)
                deadline = time.monotonic() + 1
                while len(backend.create_calls) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(set(backend.create_calls), {first.id, second.id})
                backend.ready.set()
                deadline = time.monotonic() + 2
                while len(results) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(results), 2)
                self.assertTrue(all(result.ready for result in results))
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_same_tmux_target_is_single_flight_across_session_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                None,
                max_workers=2,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = self._store_record(store, name="tmux-a")
                second = self._store_record(store, name="tmux-b")
                second = replace(second, tmux_name=first.tmux_name)
                results = []
                coordinator.submit(first, on_ready=results.append)
                deadline = time.monotonic() + 1
                while not backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                duplicate = coordinator.submit(second, on_ready=results.append)
                self.assertEqual(duplicate.state, "deduplicated")
                self.assertEqual(backend.create_calls, [first.id])
                backend.ready.set()
                deadline = time.monotonic() + 2
                while len(results) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual([result.session_id for result in results], [first.id, second.id])
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_same_auth_resume_target_is_single_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            coordinator = RecoveryCoordinator(
                backend,
                None,
                max_workers=2,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                first = replace(
                    self._store_record(store, name="resume-a", command="codex --yolo"),
                    auth_scope_id="shared-auth",
                    recovery_command="codex resume thread-42",
                )
                second = replace(
                    self._store_record(store, name="resume-b", command="codex --yolo"),
                    auth_scope_id="shared-auth",
                    recovery_command="codex resume thread-42",
                )
                results = []
                with mock.patch(
                    "termdeck.recovery_coordinator.process_tree_contains_codex",
                    return_value=True,
                ):
                    coordinator.submit(first, on_ready=results.append)
                    deadline = time.monotonic() + 1
                    while not backend.create_calls and time.monotonic() < deadline:
                        time.sleep(0.005)
                    duplicate = coordinator.submit(second, on_ready=results.append)
                    self.assertEqual(duplicate.state, "deduplicated")
                    self.assertEqual(backend.create_calls, [first.id])
                    backend.ready.set()
                    deadline = time.monotonic() + 2
                    while len(results) < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                self.assertEqual({result.session_id for result in results}, {first.id, second.id})
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_two_coordinators_share_one_durable_target_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store_a = SessionStore(path)
            store_b = SessionStore(path)
            backend = FakeBackend()
            first_coordinator = RecoveryCoordinator(
                backend,
                store_a,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            second_coordinator = RecoveryCoordinator(
                backend,
                store_b,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store_a, name="cross-process")
                first = first_coordinator.submit(record)
                deadline = time.monotonic() + 1
                while not backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                second = second_coordinator.submit(store_b.get(record.id))
                self.assertEqual(first.state, "queued")
                self.assertEqual(second.state, "deduplicated")
                self.assertEqual(backend.create_calls, [record.id])
                backend.ready.set()
                deadline = time.monotonic() + 2
                while first_coordinator.inflight_ids and time.monotonic() < deadline:
                    time.sleep(0.005)
                claim_count = store_a.conn.execute(
                    "SELECT COUNT(*) FROM recovery_claims"
                ).fetchone()[0]
                self.assertEqual(claim_count, 0)
            finally:
                backend.ready.set()
                first_coordinator.shutdown(wait=True)
                second_coordinator.shutdown(wait=True)
                store_a.close()
                store_b.close()

    def test_expired_durable_claim_can_be_recovered_by_new_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(path)
            record = self._store_record(store, name="expired-claim")
            first = RecoveryCoordinator(
                FakeBackend(),
                store,
                claim_ttl=5,
                wall_clock=lambda: 0.0,
            )
            second = RecoveryCoordinator(
                FakeBackend(),
                store,
                claim_ttl=5,
                wall_clock=lambda: 10.0,
            )
            try:
                keys = first._single_flight_keys(record)
                first_claim = first._mark_pending(record, keys)
                second_claim = second._mark_pending(record, keys)
                self.assertNotEqual(first_claim.token, second_claim.token)
                self.assertEqual(second_claim.target_keys, keys)
                second._persist_failure(
                    store,
                    record,
                    second_claim.attempt,
                    "test cleanup",
                    claim_token=second_claim.token,
                    target_keys=keys,
                )
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM recovery_claims").fetchone()[0],
                    0,
                )
            finally:
                first.shutdown(wait=True)
                second.shutdown(wait=True)
                store.close()

    def test_stale_pane_pid_without_authoritative_evidence_is_not_live(self):
        class StalePaneBackend:
            def session_exists(self, _record):
                return True

            def pane_pid(self, _record):
                return 9901

            def create_or_resume(self, _record):
                raise AssertionError("stale pane must not be attached")

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = self._store_record(store, name="stale-pane")
            coordinator = RecoveryCoordinator(
                StalePaneBackend(),
                None,
                readiness_timeout=0.01,
                poll_interval=0.005,
            )
            try:
                submission = coordinator.submit(record, known_live=True, known_pid=9901)
                self.assertEqual(submission.state, "queued")
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_payload_recovery_result_preserves_shell_bridge_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = PayloadBackend()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store, name="payload", command="bash")
                backend.live[record.id] = 8200
                results = []
                submission = coordinator.submit(
                    record,
                    mode="payload",
                    on_ready=results.append,
                )
                self.assertEqual(submission.state, "queued")
                deadline = time.monotonic() + 1
                while not results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].ready)
                self.assertEqual(results[0].action, "shell")
                self.assertEqual(results[0].pid, 8201)
                self.assertEqual(results[0].command, "/usr/bin/bash -i")
                self.assertEqual(results[0].cwd, record.cwd)
                self.assertTrue(results[0].clear_recovery)
                self.assertEqual(backend.recovery_calls, [record.id])
            finally:
                coordinator.shutdown(wait=True)
                store.close()


if __name__ == "__main__":
    unittest.main()
