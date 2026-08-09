from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest import TestCase, mock

from termdeck.gtk_app import TermDeckWindow
from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.session_reconcile import reconcile_records
from termdeck.task_snapshot import build_task_snapshot
from termdeck.tmux_backend import TmuxBackend


def _record(*, status: str = "running", last_pid: int | None = 101) -> SessionRecord:
    return SessionRecord(
        id="tab-1",
        name="agent",
        tag="ai",
        command="agent",
        cwd="/tmp",
        tmux_name="termdeck_tab_1",
        created_at="now",
        last_opened_at="now",
        status=status,
        last_pid=last_pid,
        visible=True,
    )


class MissingProcSampler:
    def snapshot(self, pid):
        return ResourceSnapshot(pid=pid, exists=False)


class FreshPaneBackend:
    available = True
    pane_snapshot_stale = False

    def __init__(self, pid: int | None):
        self.pid = pid

    def pane_pids(self, records):
        return {record.id: self.pid for record in records}

    def session_exists_many(self, records):
        return {record.id: True for record in records}


class PidNamespaceLivenessTests(TestCase):
    def test_task_snapshot_keeps_a_fresh_tmux_pane_live_when_proc_cannot_see_it(self):
        item = build_task_snapshot(
            [_record()],
            backend=FreshPaneBackend(4242),
            sampler=MissingProcSampler(),
        )[0]

        self.assertTrue(item.snapshot.exists)
        self.assertEqual(item.snapshot.state, "unknown")
        self.assertEqual(item.row.state, "running")
        self.assertEqual(item.health.label, "sampling")

    def test_task_snapshot_does_not_promote_a_remembered_pid_without_a_live_pane(self):
        item = build_task_snapshot(
            [_record(last_pid=4242)],
            backend=FreshPaneBackend(None),
            sampler=MissingProcSampler(),
        )[0]

        self.assertIsNone(item.pid)
        self.assertFalse(item.snapshot.exists)
        self.assertEqual(item.row.state, "stopped")
        self.assertEqual(item.health.label, "missing")

    def test_reconcile_keeps_backend_confirmed_pane_running_across_namespaces(self):
        result = reconcile_records(
            [_record()],
            backend=FreshPaneBackend(4242),
            sampler=MissingProcSampler(),
        )

        row = result.rows[0]
        self.assertEqual(row.after_status, "running")
        self.assertTrue(row.live_session)
        self.assertTrue(row.live_process)
        self.assertEqual(row.detail, "live session observed; resource sample deferred")

    def test_paused_state_survives_an_unobservable_confirmed_pane(self):
        item = build_task_snapshot(
            [_record(status="paused")],
            backend=FreshPaneBackend(4242),
            sampler=MissingProcSampler(),
        )[0]

        self.assertEqual(item.row.state, "paused")
        self.assertEqual(item.health.label, "paused")

    def test_observed_status_trusts_only_a_resolved_pane_pid(self):
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=SimpleNamespace(pane_snapshot_stale=False),
                sampler=MissingProcSampler(),
            )
        )

        self.assertEqual(TermDeckWindow.observed_status(window, _record(), 4242), "running")
        self.assertEqual(TermDeckWindow.observed_status(window, _record(), None), "stopped")

    def test_dead_tmux_pane_is_not_returned_as_live(self):
        backend = TmuxBackend(executable="tmux")
        dead = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="termdeck_tab_1\t0\t0\t%1\t4242\t/tmp\t1\n",
            stderr="",
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend,
            "_run",
            return_value=dead,
        ):
            self.assertIsNone(backend.pane_pids([_record()])["tab-1"])


if __name__ == "__main__":
    import unittest

    unittest.main()
