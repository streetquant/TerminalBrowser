import inspect
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from gi.repository import Gio

from termdeck import gtk_app
from termdeck.models import ResourceSnapshot
from termdeck.recovery_intent import (
    consume_pending_recovery_intents,
    request_recovery_intent,
)
from termdeck.session_store import DEFAULT_DB_PATH, SessionStore


class GuiRunBoundaryTest(TestCase):
    def test_application_exports_quit_preserving_sessions_action(self) -> None:
        source = inspect.getsource(gtk_app.TermDeckApplication.__init__)
        self.assertIn("quit-preserving-sessions", source)
        self.assertIn("Gio.SimpleAction", source)
        app = mock.Mock()
        gtk_app.TermDeckApplication._activate_quit_preserving_sessions(
            app, None, None
        )
        app.quit_preserving_sessions.assert_called_once_with()

    def test_quit_refuses_when_tmux_lifecycle_is_not_safe(self) -> None:
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = SimpleNamespace(
            ok=False, detail="user systemd is unavailable"
        )
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=True,
        ):
            self.assertFalse(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        app.quit.assert_not_called()

    def test_quit_revalidates_tmux_lifecycle_before_closing(self) -> None:
        isolation = SimpleNamespace(ok=True, detail="safe")
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = isolation
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=True,
        ):
            self.assertTrue(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        self.assertIs(app._tmux_server_isolation, isolation)
        app.quit.assert_called_once_with()

    def test_non_main_child_quit_can_preserve_sessions_without_user_systemd(self) -> None:
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = SimpleNamespace(
            ok=False, detail="user systemd is unavailable"
        )
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=False,
        ):
            self.assertTrue(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        app.quit.assert_called_once_with()

    def test_application_identity_is_stable_and_scoped_to_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.sqlite3"
            second = root / "second.sqlite3"
            first.touch()
            alias = root / "first-alias.sqlite3"
            alias.symlink_to(first)

            first_id = gtk_app.application_id_for_database(first)

            self.assertEqual(first_id, gtk_app.application_id_for_database(alias))
            self.assertNotEqual(first_id, gtk_app.application_id_for_database(second))
            self.assertRegex(first_id, r"^dev\.t1000\.T1000\.db_[0-9a-f]{32}$")
            self.assertTrue(Gio.Application.id_is_valid(first_id))
            self.assertEqual(gtk_app.canonical_database_path(alias), first.resolve())
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                gtk_app.application_id_for_database(root)

        self.assertEqual(
            gtk_app.application_id_for_database(None),
            gtk_app.application_id_for_database(DEFAULT_DB_PATH),
        )
        self.assertEqual(gtk_app.application_id_for_database(None), "dev.t1000.T1000")
        with self.assertRaisesRegex(ValueError, "file-backed"):
            gtk_app.application_id_for_database(":memory:")

    def test_run_hides_consumed_cli_options_from_gtk_application(self) -> None:
        application = mock.Mock()
        application.run.return_value = 23
        db_path = Path("/tmp/termdeck-test.sqlite3")
        workspace_path = Path("/tmp/termdeck-test.workspace.json")

        with mock.patch.object(gtk_app, "TermDeckApplication", return_value=application) as constructor:
            result = gtk_app.run(db_path=db_path, workspace_path=workspace_path)

        constructor.assert_called_once_with(db_path=db_path, workspace_path=workspace_path)
        application.run.assert_called_once_with([sys.argv[0]])
        self.assertEqual(result, 23)

    def test_primary_startup_verifies_or_migrates_managed_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "runtime" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            store = mock.Mock()
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch.object(gtk_app, "DEFAULT_DB_PATH", target), mock.patch.object(
                gtk_app, "migrate_legacy_database"
            ) as migrate, mock.patch.object(
                gtk_app, "SessionStore", return_value=store
            ) as constructor, mock.patch.object(
                gtk_app, "consume_pending_recovery_intents"
            ) as consume:
                result = gtk_app.open_primary_session_store(target)

            self.assertIs(result, store)
            migrate.assert_called_once_with(destination=target)
            constructor.assert_called_once_with(target)
            consume.assert_not_called()

    def test_existing_managed_registry_runs_ssd_local_receipt_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "runtime" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            target.parent.mkdir(parents=True)
            target.touch()
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch.object(gtk_app, "DEFAULT_DB_PATH", target), mock.patch.object(
                gtk_app, "migrate_legacy_database"
            ) as migrate, mock.patch.object(
                gtk_app, "SessionStore"
            ) as constructor, mock.patch.object(
                gtk_app, "consume_pending_recovery_intents"
            ) as consume:
                gtk_app.open_primary_session_store(target)

            migrate.assert_called_once_with(destination=target)
            constructor.assert_called_once_with(target)
            consume.assert_not_called()

    def test_migration_failure_prevents_opening_unverified_managed_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "runtime" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch.object(gtk_app, "DEFAULT_DB_PATH", target), mock.patch.object(
                gtk_app,
                "migrate_legacy_database",
                side_effect=RuntimeError("parity failed"),
            ), mock.patch.object(gtk_app, "SessionStore") as constructor, self.assertRaisesRegex(
                RuntimeError,
                "parity failed",
            ):
                gtk_app.open_primary_session_store(target)

            constructor.assert_not_called()

    def test_recovery_intent_drain_is_background_single_flight(self) -> None:
        store_path = Path("/tmp/termdeck-managed.sqlite3")
        worker_store = mock.Mock()
        consume_started = threading.Event()
        release_consume = threading.Event()
        app = SimpleNamespace(
            store=SimpleNamespace(
                path=store_path,
                auth_root=Path("/tmp/termdeck-auth"),
                account_source=Path("/tmp/codex-home"),
                provision_auth_scopes=False,
            ),
            activate=mock.Mock(),
            _recovery_intent_drain_guard=threading.Lock(),
            _recovery_intent_drain_started=False,
            _recovery_intent_refresh_pending=False,
            _recovery_intent_drain_stop=threading.Event(),
        )
        app._queue_recovery_intent_refresh = lambda: (
            gtk_app.TermDeckApplication._queue_recovery_intent_refresh(app)
        )

        def slow_consume(*args, **kwargs):
            consume_started.set()
            if not release_consume.wait(2):
                raise RuntimeError("timed out waiting to release recovery drain")
            return [
                SimpleNamespace(
                    session_id="session-one",
                    state="applied",
                    detail="test consume",
                )
            ]

        def run_idle(callback, *args):
            callback(*args)
            return 1

        started = time.monotonic()
        with mock.patch.object(
            gtk_app,
            "SessionStore",
            return_value=worker_store,
        ) as constructor, mock.patch.object(
            gtk_app,
            "recovery_intent_backlog_pending",
            side_effect=[True, False],
        ), mock.patch.object(
            gtk_app,
            "consume_pending_recovery_intents",
            side_effect=slow_consume,
        ) as consume, mock.patch.object(
            gtk_app.GLib,
            "idle_add",
            side_effect=run_idle,
        ), mock.patch.object(
            gtk_app.os,
            "nice",
        ):
            self.assertFalse(
                gtk_app.TermDeckApplication._schedule_recovery_intent_drain(app)
            )
            elapsed = time.monotonic() - started
            self.assertTrue(consume_started.wait(1))
            self.assertLess(elapsed, 0.1)
            self.assertTrue(app._recovery_intent_drain_started)
            self.assertFalse(
                gtk_app.TermDeckApplication._schedule_recovery_intent_drain(app)
            )
            constructor.assert_called_once()
            release_consume.set()
            deadline = time.monotonic() + 2
            while app._recovery_intent_drain_started and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(app._recovery_intent_drain_started)
        consume.assert_called_once_with(worker_store, time_budget_seconds=0.05)
        worker_store.close.assert_called_once_with()
        app.activate.assert_called_once_with()

    def test_startup_reconcile_cannot_invalidate_queued_running_intent(self) -> None:
        class MissingThenRecoveredBackend:
            available = True

            def __init__(self) -> None:
                self.running = False
                self.recovery_calls = 0

            def session_exists_many(self, records):
                return {record.id: self.running for record in records}

            def pane_pids(self, records):
                return {
                    record.id: (8123 if self.running else None)
                    for record in records
                }

            def pane_pid(self, _record):
                return 8123 if self.running else None

            def authoritative_live_pane_pid(self, record):
                return self.pane_pid(record)

            def create_or_resume(self, _record):
                self.recovery_calls += 1
                self.running = True

        class MissingSampler:
            @staticmethod
            def snapshot(pid):
                return ResourceSnapshot(pid=pid, exists=False)

            @staticmethod
            def snapshots(pids, *, refresh_limit=None):
                del refresh_limit
                return [ResourceSnapshot(pid=pid, exists=False) for pid in pids]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            managed_db = root.parent / "termdeck" / "sessions.sqlite3"
            managed_db.parent.mkdir(mode=0o700)
            environment = {"T1000_MANAGED_ROOT": str(root)}
            store = SessionStore(managed_db)
            try:
                record = store.create(
                    "prompts",
                    "terminal",
                    "/bin/bash",
                    "/8tb/bug-bounty/prompts",
                )
                store.update(
                    record.id,
                    status="running",
                    last_pid=4242,
                    recovery_command="codex --yolo resume startup-race",
                    recovery_cwd=record.cwd,
                    recovery_intent="inactive",
                )
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    side_effect=AssertionError("managed startup must not read legacy"),
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    request_recovery_intent(
                        record.id,
                        name=record.name,
                        cwd=record.cwd,
                        environ=environment,
                    )

                    backend = MissingThenRecoveredBackend()
                    first_reconcile = gtk_app.reconcile_store(
                        store,
                        backend=backend,
                        sampler=MissingSampler(),
                        hide_missing=True,
                        recover_missing=True,
                    )
                    self.assertEqual(first_reconcile.rows[0].after_status, "stopped")
                    self.assertFalse(store.get(record.id).visible)

                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )

                self.assertEqual([outcome.state for outcome in outcomes], ["applied"])
                claimed = store.get(record.id)
                self.assertEqual(claimed.status, "recovery-pending")
                self.assertTrue(claimed.visible)
                self.assertEqual(claimed.recovery_intent, "active")

                second_reconcile = gtk_app.reconcile_store(
                    store,
                    backend=backend,
                    sampler=MissingSampler(),
                    hide_missing=True,
                    recover_missing=True,
                )
                self.assertEqual(
                    second_reconcile.rows[0].after_status,
                    "recovery-pending",
                )
                candidates = gtk_app.startup_restore_candidates([store.get(record.id)])
                summary = gtk_app.revive_dead_payload_sessions(
                    backend,
                    candidates,
                    store=store,
                )
                self.assertEqual(summary.revived, 1)
                self.assertEqual(backend.recovery_calls, 1)
                self.assertEqual(store.get(record.id).status, "running")
            finally:
                store.close()

    def test_retryable_recovery_outcome_backs_off_without_hot_loop(self) -> None:
        class StopAfterFirstWait:
            def __init__(self) -> None:
                self.stopped = False
                self.waits: list[float] = []

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, timeout: float) -> bool:
                self.waits.append(timeout)
                self.stopped = True
                return True

            def set(self) -> None:
                self.stopped = True

        stop = StopAfterFirstWait()
        worker_store = mock.Mock()
        app = SimpleNamespace(
            store=SimpleNamespace(
                path=Path("/tmp/termdeck-managed.sqlite3"),
                auth_root=Path("/tmp/termdeck-auth"),
                account_source=Path("/tmp/codex-home"),
                provision_auth_scopes=False,
            ),
            _recovery_intent_drain_guard=threading.Lock(),
            _recovery_intent_drain_started=False,
            _recovery_intent_refresh_pending=False,
            _recovery_intent_drain_stop=stop,
        )
        app._queue_recovery_intent_refresh = mock.Mock()
        pending = [
            SimpleNamespace(
                session_id="td-busy",
                state="pending",
                detail="retry later",
            )
        ]
        with mock.patch.object(
            gtk_app,
            "SessionStore",
            return_value=worker_store,
        ), mock.patch.object(
            gtk_app,
            "recovery_intent_backlog_pending",
            return_value=True,
        ), mock.patch.object(
            gtk_app,
            "consume_pending_recovery_intents",
            return_value=pending,
        ) as consume, mock.patch.object(
            gtk_app.os,
            "nice",
        ):
            self.assertFalse(
                gtk_app.TermDeckApplication._schedule_recovery_intent_drain(app)
            )
            deadline = time.monotonic() + 2
            while app._recovery_intent_drain_started and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(app._recovery_intent_drain_started)
        consume.assert_called_once_with(worker_store, time_budget_seconds=0.05)
        self.assertEqual(stop.waits, [0.05])
        app._queue_recovery_intent_refresh.assert_not_called()
        worker_store.close.assert_called_once_with()


if __name__ == "__main__":
    import unittest

    unittest.main()
