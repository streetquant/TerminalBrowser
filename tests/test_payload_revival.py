"""Startup revival of dead or missing managed terminal payloads.

A canonical generic recipe—not an agent name, account directory, or conversation
format—is the restart authority. These tests pin candidate selection and the
fail-closed delegation to ``create_or_resume``.
"""

from __future__ import annotations

import inspect
import unittest

from termdeck.gtk_app import (
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

    def pane_pid(self, record):
        if record.id in self.pid_error_ids:
            raise TmuxError("pane lookup failed")
        return self.pids.get(record.id)

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
        revived, failed = revive_dead_payload_sessions(
            backend, [dead, alive], store=store
        )
        self.assertEqual((revived, failed), (1, 0))
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
        revived, failed = revive_dead_payload_sessions(backend, [alive], store=store)
        self.assertEqual((revived, failed), (0, 0))
        self.assertEqual(store.updates, [])

    def test_refused_replacement_counts_failed_and_continues(self):
        refused = make_record("refused")
        dead = make_record("dead")
        backend = FakeBackend(
            pids={"refused": 1, "dead": 2},
            respawn_ids={"dead"},
            fail_ids={"refused"},
        )
        revived, failed = revive_dead_payload_sessions(backend, [refused, dead])
        self.assertEqual((revived, failed), (1, 1))

    def test_pane_pid_errors_are_tolerated(self):
        record = make_record("flaky")
        backend = FakeBackend(pid_error_ids={"flaky"})
        store = FakeStore()
        revived, failed = revive_dead_payload_sessions(backend, [record], store=store)
        self.assertEqual((revived, failed), (0, 0))
        self.assertEqual(store.updates, [])

    def test_existing_recovery_pending_record_probed_as_is(self):
        pending = make_record("pending", status="recovery-pending")
        backend = FakeBackend(pids={"pending": 7}, respawn_ids={"pending"})
        revived, failed = revive_dead_payload_sessions(backend, [pending])
        self.assertEqual((revived, failed), (1, 0))
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

    def test_schedule_guard_is_single_shot(self):
        source = inspect.getsource(TermDeckApplication._schedule_payload_revival)
        self.assertIn('_payload_revival_started', source)


if __name__ == "__main__":
    unittest.main()
