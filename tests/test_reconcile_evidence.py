"""Registry reconciliation must distinguish absence, uncertainty and PID reuse."""
from dataclasses import replace
import subprocess

import pytest

from termdeck.models import SessionRecord, ResourceSnapshot
from termdeck.session_reconcile import reconcile_records, reconcile_store
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend, TmuxError
from termdeck import tmux_backend


@pytest.fixture
def record():
    return SessionRecord(id="row", name="fixture", tag="test", command="bash", cwd="/tmp",
                         tmux_name="fixture", created_at="now", last_opened_at="now",
                         status="running", last_pid=555)


def snapshot_backend(monkeypatch, output, *, code=0, error="", process=None):
    backend = TmuxBackend("/usr/bin/tmux")
    monkeypatch.setattr(backend, "_run", lambda *args, **kw: subprocess.CompletedProcess([], code, output, error))
    monkeypatch.setattr(tmux_backend.os.path, "samefile", lambda *args: True)
    monkeypatch.setattr(tmux_backend, "_parse_stat", lambda pid: process)
    return backend


@pytest.mark.parametrize("process,dead,expected", [
    ({"ppid": 123, "starttime": 999, "state": "S"}, "0", "live"),
    ({"ppid": 987, "starttime": 999, "state": "S"}, "0", "identity-mismatch"),
    ({"ppid": 123, "starttime": 999, "state": "Z"}, "0", "dead"),
    (None, "0", "dead"),
    ({"ppid": 123, "starttime": 999, "state": "S"}, "1", "dead"),
])
def test_snapshot_binds_live_pane_to_tmux_parent(record, monkeypatch, process, dead, expected):
    backend = snapshot_backend(monkeypatch, f"123\tfixture\t0\t0\t%1\t555\t{dead}\n", process=process)
    evidence = backend.reconciliation_snapshot([record])[record.id]
    assert evidence["state"] == expected
    if expected == "live":
        assert backend.reconciliation_identity_current(evidence)
        monkeypatch.setattr(tmux_backend, "_parse_stat", lambda pid: {**process, "starttime": 1001})
        assert not backend.reconciliation_identity_current(evidence)


@pytest.mark.parametrize("output", ["invalid", "123\tfixture\t0\t0\t%1\t555\tunknown", "0\tfixture\t0\t0\t%1\t555\t0"])
def test_malformed_snapshot_is_not_absence(record, monkeypatch, output):
    backend = snapshot_backend(monkeypatch, output)
    with pytest.raises(TmuxError):
        backend.reconciliation_snapshot([record])


def test_unobservable_namespace_does_not_borrow_local_pid(record, monkeypatch):
    backend = snapshot_backend(monkeypatch, "123\tfixture\t0\t0\t%1\t555\t0\n",
                               process={"ppid": 1, "starttime": 1, "state": "S"})
    monkeypatch.setattr(tmux_backend.os.path, "samefile", lambda *args: False)
    assert backend.reconciliation_snapshot([record])[record.id]["state"] == "unobservable"


@pytest.mark.parametrize("error,expected", [("no server running on /fixture/socket", "missing"),
                                             ("permission denied", "unavailable"),
                                             ("query timeout", "unavailable")])
def test_missing_server_and_query_failure_are_distinct(record, monkeypatch, error, expected):
    backend = snapshot_backend(monkeypatch, "", code=1, error=error)
    result = reconcile_records([record], backend=backend)
    row = result.rows[0]
    assert row.observation == expected
    assert not row.live_session
    assert not row.live_process
    if expected == "unavailable":
        assert row.after_status == "running"
        assert not row.changed
    else:
        assert row.after_status == "stopped"


class LiveBackend:
    def reconciliation_snapshot(self, records):
        return {record.id: {"state": "live", "pid": 555, "server_pid": 123, "start_time": 999}
                for record in records}

    def reconciliation_identity_current(self, evidence):
        return True


def test_pid_exit_during_resource_sample_is_not_running(record):
    backend = LiveBackend()
    backend.reconciliation_identity_current = lambda evidence: False
    result = reconcile_records([record], backend=backend)
    assert result.rows[0].observation == "identity-changed"
    assert result.rows[0].after_status == "stopped"
    assert not result.rows[0].live_process


def test_concurrent_explicit_stop_wins_and_result_reports_it(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    other = SessionStore(tmp_path / "sessions.sqlite3")
    try:
        record = store.create("fixture", "test", "bash", str(tmp_path))
        store.update(record.id, status="running", last_pid=111)

        class ConcurrentSampler:
            def snapshot(self, pid):
                other.update(record.id, status="stopped", last_pid=None)
                return ResourceSnapshot(pid, True, state="S")

        result = reconcile_store(store, backend=LiveBackend(), sampler=ConcurrentSampler())
        assert store.get(record.id).status == "stopped"
        assert result.changed == 0
        assert result.rows[0].observation == "concurrent-change"
        assert result.rows[0].after_status == "stopped"
        assert not result.rows[0].live_process
    finally:
        other.close()
        store.close()


def test_missing_metadata_and_auth_scope_survive_idempotent_reconcile(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    try:
        record = store.create("fixture", "test", "bash", str(tmp_path))
        store.update(record.id, status="running", last_pid=555)
        record = store.get(record.id)

        class MissingBackend:
            def reconciliation_snapshot(self, records):
                return {r.id: {"state": "missing", "pid": None} for r in records}

        first = reconcile_store(store, backend=MissingBackend(), hide_missing=False, recover_missing=True)
        second = reconcile_store(store, backend=MissingBackend(), hide_missing=False, recover_missing=True)
        assert first.rows[0].after_status == "recovery-pending"
        assert first.rows[0].recoverable
        assert second.changed == 0
        saved = store.get(record.id)
        for field in ("cwd", "window_id", "window_title", "tab_index", "auth_scope_id", "codex_home",
                      "recovery_command", "recovery_cwd", "recovery_recipe", "recovery_recipe_sha256"):
            assert getattr(saved, field) == getattr(record, field)
        partial = replace(record, recovery_recipe=None, recovery_recipe_sha256=None,
                          command="", restart_policy="manual", codex_home=None, auth_scope_id=None)
        result = reconcile_records([partial], backend=MissingBackend(), recover_missing=True)
        assert not result.rows[0].recoverable
        assert result.rows[0].after_status == "stopped"
        stale = replace(record, codex_home=str(tmp_path / "absent-scope"), auth_scope_id="absent-scope")
        stale_result = reconcile_records([stale], backend=MissingBackend(), recover_missing=True)
        assert stale_result.rows[0].auth_scope_state == "missing"
        assert stale_result.rows[0].after_status == "recovery-pending"
    finally:
        store.close()
