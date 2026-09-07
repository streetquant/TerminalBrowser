import fcntl
import os
import socket
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
from termdeck.tmux_backend import (
    TmuxBackend,
    TmuxError,
    _auth_legacy_lifetime_lock_path,
    _auth_lifetime_lock_path,
)


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
                self.assertEqual(first_results, [])
                backend.ready.set()
                deadline = time.monotonic() + 1
                while not first_results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(first_results[0].state, "failed")
                self.assertIn("shut down", first_results[0].error)
                self.assertEqual(store.get(first.id).status, "recovery-pending")
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
                resume_id = "019f0000-0000-7000-8000-000000000042"
                first = replace(
                    self._store_record(store, name="resume-a", command="codex --yolo"),
                    auth_scope_id="shared-auth",
                    recovery_command=f"codex resume {resume_id}",
                )
                second = replace(
                    self._store_record(store, name="resume-b", command="codex --yolo"),
                    auth_scope_id="shared-auth",
                    recovery_command=f"codex resume {resume_id}",
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
                self.assertEqual(backend.create_calls, [first.id, second.id])
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_two_coordinators_share_one_durable_target_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store_a = SessionStore(path)
            store_b = SessionStore(path)
            first_backend = FakeBackend()
            second_backend = FakeBackend()
            first_backend.warp_drives_ready = lambda _record: True
            second_backend.warp_drives_ready = lambda _record: True
            first_coordinator = RecoveryCoordinator(
                first_backend,
                store_a,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            second_coordinator = RecoveryCoordinator(
                second_backend,
                store_b,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(
                    store_a,
                    name="cross-process",
                    command="codex --yolo",
                )
                alias = self._store_record(
                    store_a,
                    name="cross-process-alias",
                    command="codex --yolo",
                )
                resume_command = "codex resume 019f0000-0000-7000-8000-000000000001"
                store_a.conn.execute(
                    "UPDATE sessions SET recovery_command = ? WHERE id IN (?, ?)",
                    (resume_command, record.id, alias.id),
                )
                store_a.conn.commit()
                record = store_a.get(record.id)
                alias = store_a.get(alias.id)
                assert record is not None and alias is not None
                independent = self._store_record(store_a, name="independent")
                second_results = []
                first = first_coordinator.submit(record)
                deadline = time.monotonic() + 1
                while not first_backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                second = second_coordinator.submit(
                    store_b.get(alias.id),
                    on_ready=second_results.append,
                    on_failed=second_results.append,
                )
                second_coordinator.submit(store_b.get(independent.id))
                self.assertEqual(first.state, "queued")
                self.assertEqual(second.state, "deduplicated")
                deadline = time.monotonic() + 1
                while not second_backend.create_calls and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(first_backend.create_calls, [record.id])
                self.assertEqual(second_backend.create_calls, [independent.id])
                second_backend.ready.set()
                first_backend.ready.set()
                deadline = time.monotonic() + 2
                while (
                    first_coordinator.inflight_ids
                    or second_coordinator.inflight_ids
                    or not second_results
                ) and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(second_results), 1)
                self.assertTrue(second_results[0].ready)
                self.assertEqual(second_results[0].session_id, alias.id)
                self.assertEqual(first_backend.create_calls, [record.id])
                self.assertEqual(
                    set(second_backend.create_calls),
                    {independent.id, alias.id},
                )
                claim_count = store_a.conn.execute(
                    "SELECT COUNT(*) FROM recovery_claims"
                ).fetchone()[0]
                self.assertEqual(claim_count, 0)
            finally:
                first_backend.ready.set()
                second_backend.ready.set()
                first_coordinator.shutdown(wait=True)
                second_coordinator.shutdown(wait=True)
                store_a.close()
                store_b.close()

    def test_exact_managed_auth_scope_is_reused_across_cold_recoveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(path)
            backend = FakeBackend()
            backend.ready.set()
            coordinator = RecoveryCoordinator(
                backend,
                store,
                max_workers=1,
                readiness_timeout=1,
                poll_interval=0.005,
            )
            try:
                record = self._store_record(
                    store,
                    name="managed-codex",
                    command="codex --yolo",
                )
                scope_id = "auth-managed-test"
                managed_home = store.auth_root / scope_id
                store.auth_root.mkdir(mode=0o700)
                store.auth_root.chmod(0o700)
                managed_home.mkdir(mode=0o700)
                store.conn.execute(
                    "UPDATE sessions SET auth_scope_id = ?, codex_home = ? WHERE id = ?",
                    (scope_id, str(managed_home), record.id),
                )
                store.conn.commit()
                record = store.get(record.id)
                assert record is not None
                results = []

                with (
                    mock.patch(
                        "termdeck.recovery_coordinator.process_tree_contains_codex",
                        return_value=True,
                    ),
                    mock.patch(
                        "termdeck.recovery_coordinator.rebind_auth_scope"
                    ) as rebind,
                ):
                    coordinator.submit(
                        record,
                        on_ready=results.append,
                        on_failed=results.append,
                    )
                    deadline = time.monotonic() + 2
                    while len(results) < 1 and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertEqual(len(results), 1)
                    self.assertTrue(results[0].ready)

                    backend.live.pop(record.id, None)
                    record = store.update(
                        record.id,
                        status="missing",
                        last_pid=None,
                    )
                    coordinator.submit(
                        record,
                        on_ready=results.append,
                        on_failed=results.append,
                    )
                    deadline = time.monotonic() + 2
                    while len(results) < 2 and time.monotonic() < deadline:
                        time.sleep(0.005)

                self.assertEqual(len(results), 2)
                self.assertTrue(all(result.ready for result in results))
                self.assertEqual(backend.create_calls, [record.id, record.id])
                rebind.assert_not_called()
                current = store.get(record.id)
                assert current is not None
                self.assertEqual(current.codex_home, str(managed_home))
            finally:
                backend.ready.set()
                coordinator.shutdown(wait=True)
                store.close()

    def test_missing_auth_session_is_not_authoritative_quiescence(self):
        backend = FakeBackend()
        backend._auth_session_exists = mock.Mock(return_value=False)
        coordinator = RecoveryCoordinator(backend, None)
        try:
            record = SimpleNamespace(id="missing-auth-writer")
            missing, proof = coordinator._auth_scope_rebind_proof(record)

            self.assertTrue(missing)
            self.assertTrue(proof.missing)
            self.assertTrue(proof.dead)
            self.assertFalse(proof.quiescent)
            backend._auth_session_exists.assert_not_called()

            backend.auth_scope_writer_quiescent = mock.Mock(
                return_value={"quiescent": True}
            )
            _missing, authoritative = coordinator._auth_scope_rebind_proof(record)
            self.assertTrue(authoritative.complete)
        finally:
            coordinator.shutdown(wait=True)

    def test_production_quiescence_fence_is_held_through_legacy_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            legacy_home = root / "auth-legacy"
            legacy_home.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            backend = TmuxBackend(executable="tmux")
            coordinator = RecoveryCoordinator(backend, store)
            try:
                record = self._store_record(
                    store,
                    name="legacy-codex",
                    command="codex --yolo",
                )
                record = store.update(
                    record.id,
                    status="recovery-pending",
                    recovery_command=(
                        "codex resume 019f0000-0000-7000-8000-000000000001"
                    ),
                    recovery_cwd=record.cwd,
                )
                store.conn.execute(
                    "UPDATE sessions SET auth_scope_id = ?, codex_home = ? WHERE id = ?",
                    ("auth-legacy", str(legacy_home), record.id),
                )
                store.conn.commit()
                record = store.get(record.id)
                assert record is not None
                request = SimpleNamespace(
                    claim_token="claim-token",
                    target_keys=(f"session:{record.id}", "auth:auth-legacy"),
                )

                def guarded_rebind(_record, _store, **kwargs):
                    proof = kwargs["proof"]
                    self.assertTrue(proof.complete)
                    self.assertEqual(kwargs["auth_root"], store.auth_root)
                    self.assertEqual(kwargs["canonical_home"], store.account_source)
                    for path in (
                        _auth_legacy_lifetime_lock_path(record),
                        _auth_lifetime_lock_path(record),
                    ):
                        descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
                        try:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(
                                    descriptor,
                                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                                )
                        finally:
                            os.close(descriptor)
                    return SimpleNamespace(ok=True)

                with mock.patch.dict(
                    os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
                ), mock.patch.object(
                    backend, "_auth_session_exists", side_effect=(False, False)
                ), mock.patch.object(
                    backend, "session_exists", return_value=False
                ), mock.patch.object(
                    backend, "authoritative_live_pane_pid", return_value=None
                ), mock.patch(
                    "termdeck.recovery_coordinator.rebind_auth_scope",
                    side_effect=guarded_rebind,
                ) as rebind, mock.patch(
                    "termdeck.tmux_backend.os.kill"
                ) as kill:
                    rebound = coordinator._maybe_rebind_auth_scope(
                        record,
                        request,
                        store,
                    )

                self.assertEqual(rebound.id, record.id)
                rebind.assert_called_once()
                kill.assert_not_called()
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_active_legacy_writer_blocks_rebind_without_session_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            legacy_home = root / "auth-legacy"
            legacy_home.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            backend = TmuxBackend(executable="tmux")
            coordinator = RecoveryCoordinator(backend, store)
            try:
                record = self._store_record(
                    store,
                    name="active-legacy-codex",
                    command="codex --yolo",
                )
                record = store.update(
                    record.id,
                    status="recovery-pending",
                    recovery_command=(
                        "codex resume 019f0000-0000-7000-8000-000000000002"
                    ),
                    recovery_cwd=record.cwd,
                )
                store.conn.execute(
                    "UPDATE sessions SET auth_scope_id = ?, codex_home = ? WHERE id = ?",
                    ("auth-legacy", str(legacy_home), record.id),
                )
                store.conn.commit()
                record = store.get(record.id)
                assert record is not None
                request = SimpleNamespace(
                    claim_token="claim-token",
                    target_keys=(f"session:{record.id}", "auth:auth-legacy"),
                )

                with mock.patch.dict(
                    os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
                ):
                    legacy_lock = _auth_legacy_lifetime_lock_path(record)
                    legacy_lock.parent.mkdir(parents=True, mode=0o700)
                    holder = os.open(
                        legacy_lock,
                        os.O_RDWR | os.O_CREAT,
                        0o600,
                    )
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        with mock.patch.object(
                            backend, "_auth_session_exists", return_value=False
                        ), mock.patch.object(
                            backend, "session_exists", return_value=False
                        ), mock.patch.object(
                            backend, "authoritative_live_pane_pid", return_value=None
                        ), mock.patch(
                            "termdeck.recovery_coordinator.rebind_auth_scope"
                        ) as rebind, mock.patch(
                            "termdeck.tmux_backend.os.kill"
                        ) as kill, self.assertRaisesRegex(TmuxError, "still owns"):
                            coordinator._maybe_rebind_auth_scope(
                                record,
                                request,
                                store,
                            )
                        rebind.assert_not_called()
                        kill.assert_not_called()
                    finally:
                        fcntl.flock(holder, fcntl.LOCK_UN)
                        os.close(holder)

                current = store.get(record.id)
                assert current is not None
                self.assertEqual(current.status, "recovery-pending")
                self.assertEqual(current.codex_home, str(legacy_home))
                self.assertEqual(current.auth_scope_id, "auth-legacy")
            finally:
                coordinator.shutdown(wait=True)
                store.close()

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

    def test_foreign_claim_follower_takes_over_after_owner_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            store_a = SessionStore(path)
            store_b = SessionStore(path)
            backend = FakeBackend()
            backend.ready.set()
            now = [0.0]
            first = RecoveryCoordinator(
                FakeBackend(),
                store_a,
                claim_ttl=5,
                wall_clock=lambda: now[0],
            )
            second = RecoveryCoordinator(
                backend,
                store_b,
                claim_ttl=5,
                wall_clock=lambda: now[0],
                poll_interval=0.005,
            )
            try:
                record = self._store_record(store_a, name="expired-follower")
                keys = first._single_flight_keys(record)
                claim = first._mark_pending(record, keys)
                self.assertIsNotNone(claim)
                results = []

                submission = second.submit(
                    store_b.get(record.id),
                    on_ready=results.append,
                    on_failed=results.append,
                )
                self.assertEqual(submission.state, "deduplicated")
                now[0] = 6.0

                deadline = time.monotonic() + 2
                while not results and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].ready)
                self.assertEqual(backend.create_calls, [record.id])
                self.assertEqual(
                    store_a.conn.execute(
                        "SELECT COUNT(*) FROM recovery_claims"
                    ).fetchone()[0],
                    0,
                )
            finally:
                first.shutdown(wait=True)
                second.shutdown(wait=True)
                store_a.close()
                store_b.close()

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
                record = store.update(
                    record.id,
                    recovery_command=(
                        "codex resume 019f0000-0000-7000-8000-000000000099"
                    ),
                    recovery_cwd=record.cwd,
                )
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
                persisted = store.get(record.id)
                self.assertEqual(persisted.command, "/usr/bin/bash -i")
                self.assertEqual(persisted.cwd, record.cwd)
                self.assertIsNone(persisted.recovery_command)
                self.assertIsNone(persisted.recovery_cwd)
                self.assertEqual(persisted.restart_policy, "on-failure")
                self.assertTrue(persisted.recovery_recipe)
                self.assertTrue(persisted.recovery_recipe_sha256)
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_resume_target_keys_use_canonical_uuid_not_command_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            coordinator = RecoveryCoordinator(FakeBackend(), None)
            try:
                first = replace(
                    self._store_record(store, name="canonical-a", command="codex"),
                    auth_scope_id="shared-auth",
                    recovery_command=(
                        "codex --yolo resume 019F0000-0000-7000-8000-000000000123"
                    ),
                )
                second = replace(
                    self._store_record(store, name="canonical-b", command="codex"),
                    auth_scope_id="shared-auth",
                    recovery_command=(
                        "codex resume=ignored --resume="
                        "019f0000-0000-7000-8000-000000000123 --yolo"
                    ),
                )

                first_keys = {
                    key for key in coordinator._single_flight_keys(first)
                    if key.startswith("auth-resume:")
                }
                second_keys = {
                    key for key in coordinator._single_flight_keys(second)
                    if key.startswith("auth-resume:")
                }

                self.assertEqual(len(first_keys), 1)
                self.assertEqual(first_keys, second_keys)
            finally:
                coordinator.shutdown(wait=True)
                store.close()

    def test_pid_reuse_does_not_keep_stale_same_host_claim_alive(self):
        owner_id = (
            f"{socket.gethostname()}:{os.getpid()}:stale-start:"
            "current-boot:owner"
        )
        with mock.patch.object(
            RecoveryCoordinator,
            "_process_start_token",
            return_value="current-start",
        ), mock.patch.object(
            RecoveryCoordinator,
            "_boot_id",
            return_value="current-boot",
        ):
            self.assertFalse(RecoveryCoordinator._claim_owner_alive(owner_id))

    def test_writer_locks_are_target_scoped_not_workspace_wide(self):
        class BlockingBackend(FakeBackend):
            def __init__(self, pid):
                super().__init__()
                self.pid = pid
                self.entered = threading.Event()
                self.release = threading.Event()

            def create_or_resume(self, record):
                self.create_calls.append(record.id)
                self.entered.set()
                self.release.wait(3)
                self.live[record.id] = self.pid

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"TERMDECK_TMUX_RUNTIME": str(Path(tmp) / "tmux-runtime")},
            clear=False,
        ):
            store = SessionStore(Path(tmp) / "records.sqlite3")
            first_backend = BlockingBackend(9101)
            alias_backend = BlockingBackend(9102)
            independent_backend = BlockingBackend(9103)
            first = RecoveryCoordinator(first_backend, None, poll_interval=0.005)
            alias = RecoveryCoordinator(alias_backend, None, poll_interval=0.005)
            independent = RecoveryCoordinator(
                independent_backend,
                None,
                poll_interval=0.005,
            )
            try:
                first_record = self._store_record(store, name="scope-first")
                alias_record = replace(
                    self._store_record(store, name="scope-alias"),
                    tmux_name=first_record.tmux_name,
                )
                independent_record = self._store_record(
                    store,
                    name="scope-independent",
                )

                first.submit(first_record)
                self.assertTrue(first_backend.entered.wait(1))
                alias.submit(alias_record)
                independent.submit(independent_record)

                self.assertTrue(independent_backend.entered.wait(1))
                self.assertFalse(alias_backend.entered.is_set())
                independent_backend.release.set()
                first_backend.release.set()
                self.assertTrue(alias_backend.entered.wait(2))
                alias_backend.release.set()
            finally:
                first_backend.release.set()
                alias_backend.release.set()
                independent_backend.release.set()
                first.shutdown(wait=True)
                alias.shutdown(wait=True)
                independent.shutdown(wait=True)
                store.close()

    def test_claim_heartbeat_prevents_takeover_during_slow_backend_work(self):
        class BlockingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def create_or_resume(self, record):
                self.create_calls.append(record.id)
                self.entered.set()
                self.release.wait(3)
                self.live[record.id] = 9201

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"TERMDECK_TMUX_RUNTIME": str(Path(tmp) / "tmux-runtime")},
            clear=False,
        ):
            path = Path(tmp) / "sessions.sqlite3"
            store_a = SessionStore(path)
            store_b = SessionStore(path)
            first_backend = BlockingBackend()
            second_backend = FakeBackend()
            second_backend.ready.set()
            first = RecoveryCoordinator(first_backend, store_a, poll_interval=0.005)
            second = RecoveryCoordinator(second_backend, store_b, poll_interval=0.005)
            first.claim_ttl = 0.3
            second.claim_ttl = 0.3
            try:
                record = self._store_record(store_a, name="heartbeat")
                first.submit(record)
                self.assertTrue(first_backend.entered.wait(1))
                time.sleep(0.55)

                submission = second.submit(store_b.get(record.id))

                self.assertEqual(submission.state, "deduplicated")
                self.assertEqual(second_backend.create_calls, [])
                first_backend.release.set()
                deadline = time.monotonic() + 2
                while second.inflight_ids and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(second_backend.create_calls, [record.id])
            finally:
                first_backend.release.set()
                first.shutdown(wait=True)
                second.shutdown(wait=True)
                store_a.close()
                store_b.close()


if __name__ == "__main__":
    unittest.main()
