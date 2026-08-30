"""Startup revival of dead or missing managed terminal payloads.

A canonical generic recipe—not an agent name, account directory, or conversation
format—is the restart authority. These tests pin candidate selection and the
fail-closed delegation to ``create_or_resume``.
"""

from __future__ import annotations

import inspect
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import termdeck.gtk_app as gtk_app
from termdeck.gtk_app import (
    PayloadRevivalSummary,
    TermDeckApplication,
    payload_revival_candidates,
    revive_dead_payload_sessions,
)
from termdeck.models import SessionRecord
from termdeck.tmux_backend import TmuxError


def make_record(record_id: str, **overrides) -> SessionRecord:
    fields = dict(
        id=record_id,
        name=record_id,
        tag="terminal",
        command="/bin/bash",
        cwd="/tmp",
        tmux_name=f"termdeck_terminal_{record_id}",
        created_at="2026-08-16T00:00:00Z",
        last_opened_at="2026-08-16T00:00:00Z",
        status="running",
        recovery_command="codex --yolo resume 019fe730-fb6e-7b53-ab48-1ecad7da8234",
        recovery_cwd="/tmp",
        codex_home=f"/tmp/auth-{record_id}",
        auth_scope_id=f"scope-{record_id}",
    )
    fields.update(overrides)
    return SessionRecord(**fields)


class FakeBackend:
    """Stands in for TmuxBackend: pids plus a recording create_or_resume."""

    def __init__(self, pids=None, respawn_ids=(), fail_ids=(), pid_error_ids=()):
        self.pids = dict(pids or {})
        self.respawn_ids = set(respawn_ids)
        self.fail_ids = set(fail_ids)
        self.pid_error_ids = set(pid_error_ids)
        self.probes = []
        self.authoritative_probes = []

    def pane_pid(self, record):
        if record.id in self.pid_error_ids:
            raise TmuxError("pane lookup failed")
        return self.pids.get(record.id)

    def authoritative_live_pane_pid(self, record):
        self.authoritative_probes.append(record.id)
        return self.pane_pid(record)

    def create_or_resume(self, record):
        self.probes.append((record.id, record.status))
        if record.id in self.fail_ids:
            raise TmuxError("refused to replace pane")
        if record.id in self.respawn_ids:
            self.pids[record.id] = self.pids.get(record.id, 0) + 1000


class FakeStore:
    def __init__(self):
        self.updates = []

    def update(self, session_id, **kwargs):
        self.updates.append((session_id, kwargs))


class PayloadRevivalCandidateTests(unittest.TestCase):
    def test_candidates_require_valid_automatic_generic_recipe(self):
        good = make_record("good")
        manual = make_record("manual", restart_policy="manual")
        corrupt = make_record(
            "corrupt",
            recovery_recipe="{}",
            recovery_recipe_sha256="0" * 64,
        )
        rows = payload_revival_candidates(
            [
                good,
                make_record("external", external=True),
                make_record("stopped", status="stopped"),
                make_record("archived", status="archived"),
                # Optional account/native-resume metadata is not required; the
                # original command/cwd still form a generic legacy recipe.
                make_record("no-home", codex_home=""),
                make_record("no-cmd", recovery_command=None),
                make_record("no-cwd", recovery_cwd="  "),
                manual,
                corrupt,
            ]
        )
        self.assertEqual(
            [record.id for record in rows],
            ["good", "no-cmd", "no-cwd", "no-home"],
        )

    def test_candidates_order_visible_tabs_first(self):
        hidden = make_record("hidden", visible=False, tab_index=0)
        late = make_record("late", tab_index=4)
        early = make_record("early", tab_index=1)
        rows = payload_revival_candidates([hidden, late, early])
        self.assertEqual([record.id for record in rows], ["early", "late", "hidden"])


class ReviveDeadPayloadTests(unittest.TestCase):
    def test_revives_only_dead_payloads(self):
        dead = make_record("dead")
        alive = make_record("alive")
        backend = FakeBackend(pids={"dead": 100, "alive": 200}, respawn_ids={"dead"})
        store = FakeStore()
        summary = revive_dead_payload_sessions(backend, [dead, alive], store=store)
        self.assertEqual((summary.revived, summary.repaired, summary.failed), (1, 0, 0))
        self.assertEqual(
            backend.probes,
            [("dead", "recovery-pending"), ("alive", "recovery-pending")],
        )
        self.assertEqual(len(store.updates), 1)
        session_id, kwargs = store.updates[0]
        self.assertEqual(session_id, "dead")
        self.assertEqual(kwargs["status"], "running")
        self.assertEqual(kwargs["last_pid"], 1100)
        self.assertIn("last_opened_at", kwargs)

    def test_alive_noop_touches_nothing(self):
        alive = make_record("alive")
        backend = FakeBackend(pids={"alive": 321})
        store = FakeStore()
        summary = revive_dead_payload_sessions(backend, [alive], store=store)
        self.assertEqual((summary.revived, summary.repaired, summary.failed), (0, 0, 0))
        self.assertEqual(store.updates, [])

    def test_live_recovery_pending_row_is_repaired_without_replacing_payload(self):
        pending = make_record(
            "pending-live",
            status="recovery-pending",
            last_pid=321,
            recovery_last_error="managed pane is not authoritatively dead",
        )
        backend = FakeBackend(pids={pending.id: 321})
        store = FakeStore()

        summary = revive_dead_payload_sessions(backend, [pending], store=store)

        self.assertEqual((summary.revived, summary.repaired, summary.failed), (0, 1, 0))
        self.assertEqual(backend.probes, [(pending.id, "recovery-pending")])
        self.assertEqual(backend.authoritative_probes, [pending.id, pending.id])
        self.assertEqual(
            store.updates,
            [
                (
                    pending.id,
                    {
                        "status": "running",
                        "last_pid": 321,
                        "recovery_last_error": None,
                        "last_opened_at": store.updates[0][1]["last_opened_at"],
                    },
                )
            ],
        )

    def test_contradictory_row_is_not_repaired_from_stale_cached_pid(self):
        pending = make_record(
            "stale-cache",
            status="recovery-pending",
            last_pid=321,
            recovery_last_error="stale",
        )

        class StaleCacheBackend(FakeBackend):
            def pane_pid(self, record):
                return 321

            def authoritative_live_pane_pid(self, record):
                self.authoritative_probes.append(record.id)
                return None

        backend = StaleCacheBackend()
        store = FakeStore()

        summary = revive_dead_payload_sessions(backend, [pending], store=store)

        self.assertEqual((summary.revived, summary.repaired, summary.failed), (0, 0, 0))
        self.assertEqual(backend.authoritative_probes, [pending.id, pending.id])
        self.assertEqual(store.updates, [])

    def test_refused_replacement_counts_failed_and_continues(self):
        refused = make_record("refused")
        dead = make_record("dead")
        backend = FakeBackend(
            pids={"refused": 1, "dead": 2},
            respawn_ids={"dead"},
            fail_ids={"refused"},
        )
        summary = revive_dead_payload_sessions(backend, [refused, dead])
        self.assertEqual((summary.revived, summary.repaired, summary.failed), (1, 0, 1))

    def test_pane_pid_errors_are_tolerated(self):
        record = make_record("flaky")
        backend = FakeBackend(pid_error_ids={"flaky"})
        store = FakeStore()
        summary = revive_dead_payload_sessions(backend, [record], store=store)
        self.assertEqual((summary.revived, summary.repaired, summary.failed), (0, 0, 0))
        self.assertEqual(store.updates, [])

    def test_existing_recovery_pending_record_probed_as_is(self):
        pending = make_record("pending", status="recovery-pending")
        backend = FakeBackend(pids={"pending": 7}, respawn_ids={"pending"})
        summary = revive_dead_payload_sessions(backend, [pending])
        self.assertEqual((summary.revived, summary.repaired, summary.failed), (1, 0, 0))
        self.assertEqual(backend.probes, [("pending", "recovery-pending")])


class SchedulingTests(unittest.TestCase):
    def test_timeout_runs_sweep_once_and_stops(self):
        calls = []

        class FakeApp:
            _schedule_payload_revival = lambda self: calls.append(True)

        result = TermDeckApplication._payload_revival_timeout(FakeApp())
        self.assertIs(result, False)
        self.assertEqual(calls, [True])

    def test_do_activate_schedules_revival(self):
        source = inspect.getsource(TermDeckApplication.do_activate)
        self.assertIn("_payload_revival_timeout", source)
        self.assertIn("GLib.timeout_add_seconds(2, payload_revival_timeout)", source)

    def _app(self, records=None):
        app = TermDeckApplication.__new__(TermDeckApplication)
        app._payload_revival_lock = threading.Lock()
        app._payload_revival_started = False
        app.backend = object()
        app.windows = []
        stored_records = [make_record("dead")] if records is None else list(records)
        app.store = SimpleNamespace(
            list=mock.Mock(return_value=stored_records),
            path=None,
        )
        return app

    def test_second_sweep_runs_after_first_worker_finishes(self):
        app = self._app()
        threads = []

        class DeferredThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target
                threads.append(self)

            def start(self):
                return None

        with mock.patch.object(gtk_app.threading, "Thread", DeferredThread), mock.patch.object(
            gtk_app,
            "revive_dead_payload_sessions",
            return_value=PayloadRevivalSummary(),
        ):
            TermDeckApplication._schedule_payload_revival(app)
            TermDeckApplication._schedule_payload_revival(app)
            self.assertEqual(len(threads), 1)
            self.assertTrue(app._payload_revival_started)

            threads[0].target()
            self.assertFalse(app._payload_revival_started)

            TermDeckApplication._schedule_payload_revival(app)
            self.assertEqual(len(threads), 2)

    def test_worker_failure_releases_sweep_guard(self):
        app = self._app()
        threads = []

        class DeferredThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target
                threads.append(self)

            def start(self):
                return None

        with mock.patch.object(gtk_app.threading, "Thread", DeferredThread), mock.patch.object(
            gtk_app,
            "revive_dead_payload_sessions",
            side_effect=RuntimeError("worker failed"),
        ):
            TermDeckApplication._schedule_payload_revival(app)
            threads[0].target()
            self.assertFalse(app._payload_revival_started)
            TermDeckApplication._schedule_payload_revival(app)
            self.assertEqual(len(threads), 2)

    def test_store_failures_and_empty_candidates_release_sweep_guard(self):
        app = self._app(records=[])
        TermDeckApplication._schedule_payload_revival(app)
        self.assertFalse(app._payload_revival_started)
        app.store.list.return_value = [make_record("again")]

        threads = []

        class DeferredThread:
            def __init__(self, *, target, **_kwargs):
                threads.append(target)

            def start(self):
                return None

        with mock.patch.object(gtk_app.threading, "Thread", DeferredThread):
            app.store.list.side_effect = RuntimeError("store failed")
            TermDeckApplication._schedule_payload_revival(app)
            self.assertFalse(app._payload_revival_started)
            self.assertEqual(threads, [])

    def test_idle_publish_failure_releases_sweep_guard(self):
        app = self._app()
        threads = []

        class DeferredThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target
                threads.append(self)

            def start(self):
                return None

        with mock.patch.object(gtk_app.threading, "Thread", DeferredThread), mock.patch.object(
            gtk_app,
            "revive_dead_payload_sessions",
            return_value=PayloadRevivalSummary(revived=1),
        ), mock.patch.object(
            gtk_app.GLib,
            "idle_add",
            side_effect=RuntimeError("GTK is shutting down"),
        ):
            TermDeckApplication._schedule_payload_revival(app)
            threads[0].target()
            self.assertFalse(app._payload_revival_started)
            TermDeckApplication._schedule_payload_revival(app)
            self.assertEqual(len(threads), 2)

    def test_activation_skips_legacy_archive_scan_but_schedules_payload_revival(self):
        class FakeGLib:
            scheduled = []

            @classmethod
            def timeout_add_seconds(cls, delay, callback):
                cls.scheduled.append((delay, callback))
                return len(cls.scheduled)

        app = TermDeckApplication.__new__(TermDeckApplication)
        app.backend = SimpleNamespace(available=True)
        app.store = SimpleNamespace(windows_for_restore=lambda: [])
        app.sampler = object()
        app.windows = []
        app.workspace_path = None
        app._schedule_legacy_recovery = mock.Mock()
        app._payload_revival_timeout = mock.Mock(return_value=False)
        app.prompt_for_new_window = mock.Mock()

        with mock.patch.object(gtk_app, "GLib", FakeGLib), mock.patch.object(
            gtk_app, "reconcile_store"
        ):
            TermDeckApplication.do_activate(app)

        app._schedule_legacy_recovery.assert_not_called()
        self.assertEqual(len(FakeGLib.scheduled), 1)
        self.assertEqual(FakeGLib.scheduled[0][0], 2)
        FakeGLib.scheduled[0][1]()
        app._payload_revival_timeout.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
