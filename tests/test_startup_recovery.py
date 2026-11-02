from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest

from termdeck import storage_fabric
from termdeck.recovery_coordinator import RecoveryCoordinator
from termdeck.session_store import SessionStore
from termdeck.startup_recovery import recover_saved_terminals, startup_lock
from termdeck.tmux_backend import TmuxBackend


class Backend:
    def __init__(self):
        self.live = {}
        self.observations = {}
        self.created = []
        self.identity_current = True

    def reconciliation_snapshot(self, records):
        return {r.id: self.observations.get(r.id, {"state": "missing", "pid": None})
                for r in records}

    def reconciliation_identity_current(self, _evidence):
        return self.identity_current

    def authoritative_live_pane_pid(self, record):
        return self.live.get(record.id)

    pane_pid = authoritative_live_pane_pid

    def session_exists(self, record):
        return record.id in self.live

    def create_or_resume(self, record):
        self.created.append(record.id)
        self.live[record.id] = 7000 + len(self.created)


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    yield value
    value.close()


def record(store, tmp_path, name="saved", **changes):
    item = store.create(name, "dev", "/usr/bin/bash --noprofile --norc -i", str(tmp_path))
    return store.update(item.id, status="running", **changes)


def run(store, backend, tmp_path, **kwargs):
    return recover_saved_terminals(store, backend, runtime_root=tmp_path / "runtime",
                                  readiness_timeout=0.5, boot_id="owned-test-boot", **kwargs)


def test_missing_owner_recovers_without_gui_and_preserves_inactive_rows(store, tmp_path):
    backend = Backend()
    active = record(store, tmp_path, last_pid=os.getpid())  # Reused saved PID is not a live pane.
    stopped = record(store, tmp_path, "stopped")
    archived = record(store, tmp_path, "archived")
    stopped = store.update(stopped.id, status="stopped")
    archived = store.update(archived.id, status="archived")
    before = {r.id: asdict(r) for r in (stopped, archived)}
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"ready": 1, "skipped": 2}
    assert backend.created == [active.id]
    assert store.get(active.id).last_pid == backend.live[active.id]
    assert {key: asdict(store.get(key)) for key in before} == before
    assert store.get(active.id).codex_home is None


def test_exited_launcher_cannot_remain_ready_in_final_worker_result(store, tmp_path, monkeypatch):
    backend = Backend()
    item = record(store, tmp_path)
    shutdown = RecoveryCoordinator.shutdown

    def exit_after_callback(coordinator, *args, **kwargs):
        result = shutdown(coordinator, *args, **kwargs)
        backend.live.clear()
        return result

    monkeypatch.setattr(RecoveryCoordinator, "shutdown", exit_after_callback)
    result = run(store, backend, tmp_path)
    assert result["worker_quiescent"] is True
    assert result["all_eligible_ready"] is False
    assert result["counts"] == {"failed": 1}
    assert result["sessions"][0]["reason"] == "payload-exited-after-readiness"
    journal = json.loads((tmp_path / "runtime/startup-recovery-attempts.json").read_text())
    assert journal["attempts"][item.id]["state"] == "failed"


def test_completed_saved_application_preserves_recipe_without_rerun(store, tmp_path, monkeypatch):
    root = tmp_path / "tm"
    root.mkdir(mode=0o700)
    socket_root = root / f"tmux-{os.geteuid()}"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "default"
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    for key in ("TMUX", "TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_TMUX_RUNTIME_BINDING", None)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", None)
    marker = tmp_path / "application-runs"
    import shlex
    command = "printf application >> " + shlex.quote(str(marker))
    item = store.create("owned-completed", "test", command, str(tmp_path))
    item = store.update(item.id, status="running")
    base = ["/usr/bin/tmux", "-S", str(socket_path)]
    try:
        subprocess.run(base + ["new-session", "-d", "-s", "owned-seed", "/usr/bin/sleep", "60"],
                       check=True, capture_output=True, timeout=5)
        subprocess.run(base + ["set-option", "-g", "remain-on-exit", "on"],
                       check=True, capture_output=True, timeout=5)
        subprocess.run(base + ["new-session", "-d", "-s", item.tmux_name,
                              "/bin/bash", "--noprofile", "--norc", "-c", command],
                       check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            observed = subprocess.run(base + ["display-message", "-p", "-t", item.tmux_name,
                                             "#{pane_id} #{pane_dead} #{pane_dead_status}"],
                                      check=True, capture_output=True, text=True, timeout=5).stdout.split()
            if observed[1:] == ["1", "0"]:
                break
            time.sleep(0.02)
        assert observed[1:] == ["1", "0"]
        pane_id = observed[0]
        result = run(store, TmuxBackend(executable="/usr/bin/tmux"), tmp_path)
        assert result["all_eligible_ready"] is True
        assert result["sessions"][0]["reason"] == "application-completed"
        assert marker.read_text() == "application"
        observed = subprocess.run(base + ["display-message", "-p", "-t", item.tmux_name,
                                         "#{pane_id} #{pane_dead}"],
                                  check=True, capture_output=True, text=True, timeout=5).stdout.split()
        assert observed == [pane_id, "1"]
        assert store.get(item.id).command == command
        assert store.get(item.id).recovery_recipe == item.recovery_recipe
        assert store.get(item.id).status == "stopped"
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, timeout=5)


def test_live_owner_is_unchanged_even_with_unavailable_saved_cwd(store, tmp_path):
    item = record(store, tmp_path)
    item = store.update(item.id, recovery_cwd=str(tmp_path / "gone"))
    before = asdict(item)
    backend = Backend()
    backend.observations[item.id] = {"state": "live", "pid": 8123, "start_time": 99}
    backend.live[item.id] = 8123
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"already-live": 1}
    assert backend.created == []
    assert asdict(store.get(item.id)) == before


@pytest.mark.parametrize("observation,current,reason", [
    ("live", False, "pane-identity-changed"),
    ("identity-mismatch", True, "pane-owner-unobservable"),
    ("unobservable", True, "pane-owner-unobservable"),
])
def test_foreign_or_reused_live_identity_never_restarts(store, tmp_path, observation, current, reason):
    item = record(store, tmp_path)
    before = asdict(item)
    backend = Backend()
    backend.observations[item.id] = {"state": observation, "pid": os.getpid()}
    backend.identity_current = current
    result = run(store, backend, tmp_path)
    assert result["sessions"][0]["reason"] == reason
    assert backend.created == []
    assert asdict(store.get(item.id)) == before


def test_missing_cwd_blocks_only_its_tab(store, tmp_path):
    backend = Backend()
    healthy = record(store, tmp_path, "healthy")
    missing = record(store, tmp_path, "missing", recovery_cwd=str(tmp_path / "absent"))
    before = asdict(missing)
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"ready": 1, "blocked": 1}
    assert backend.created == [healthy.id]
    assert asdict(store.get(missing.id)) == before
    assert next(r for r in result["sessions"] if r["id"] == missing.id)["reason"] == "saved-working-directory-unavailable"


def test_invalid_recipe_is_isolated_and_original_receipt_is_preserved(store, tmp_path):
    backend = Backend()
    healthy = record(store, tmp_path, "healthy")
    invalid = record(store, tmp_path, "invalid")
    store.conn.execute("UPDATE sessions SET recovery_recipe_sha256 = ? WHERE id = ?",
                       ("0" * 64, invalid.id))
    store.conn.commit()
    original = store.conn.execute("SELECT recovery_recipe, recovery_recipe_sha256 FROM sessions WHERE id = ?", (invalid.id,)).fetchone()
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"ready": 1, "blocked": 1}
    assert backend.created == [healthy.id]
    assert next(r for r in result["sessions"] if r["id"] == invalid.id)["reason"] == "saved-recipe-invalid"
    assert tuple(store.conn.execute("SELECT recovery_recipe, recovery_recipe_sha256 FROM sessions WHERE id = ?", (invalid.id,)).fetchone()) == tuple(original)


def test_unchanged_failed_attempt_is_not_replayed_after_backend_restart(store, tmp_path):
    item = record(store, tmp_path)
    backend = Backend()

    def fail(record):
        backend.created.append(record.id)
        raise RuntimeError("private command and prompt must not be output")

    backend.create_or_resume = fail
    first = run(store, backend, tmp_path)
    second = run(store, backend, tmp_path)
    assert first["counts"] == {"failed": 1}
    assert second["sessions"][0]["reason"] == "already-attempted-this-boot"
    assert backend.created == [item.id]
    assert "private command" not in json.dumps(first)
    assert "private command" not in (tmp_path / "runtime/startup-recovery-attempts.json").read_text()


def test_existing_worker_serializes_backend_restart(store, tmp_path):
    backend = Backend()
    record(store, tmp_path)
    with startup_lock(tmp_path / "runtime") as acquired:
        assert acquired
        assert run(store, backend, tmp_path)["state"] == "already-owned"
    assert backend.created == []


def test_no_rebind_headless_option_keeps_gui_default_and_rejects_legacy_scope(store, tmp_path):
    item = record(store, tmp_path)
    item = replace(item, command="codex resume 00000000-0000-0000-0000-000000000001",
                   recovery_command="codex resume 00000000-0000-0000-0000-000000000001",
                   auth_scope_id="auth-legacy", codex_home=str(tmp_path / "legacy"))
    normal = RecoveryCoordinator(Backend(), store)
    headless = RecoveryCoordinator(Backend(), store, allow_auth_scope_rebind=False)
    try:
        assert normal.allow_auth_scope_rebind is True
        with pytest.raises(Exception, match="automatic rebind is disabled"):
            headless._maybe_rebind_auth_scope(item, None, store)
        assert not (tmp_path / "legacy").exists()
    finally:
        normal.shutdown(wait=True)
        headless.shutdown(wait=True)


def test_headless_keeps_optional_account_contract_for_unbound_opaque_launcher(store, tmp_path, monkeypatch):
    command = "codex-opencode resume 00000000-0000-0000-0000-000000000001"
    item = store.create("opaque-saved", "work", command, str(tmp_path))
    item = store.update(item.id, status="running")
    backend = Backend()
    # This control tests admission/preserved launch metadata, not a provider.
    monkeypatch.setattr(RecoveryCoordinator, "_payload_ready", lambda self, record, pid: True)
    result = run(store, backend, tmp_path)
    assert result["counts"] == {"ready": 1}
    assert backend.created == [item.id]
    current = store.get(item.id)
    assert (current.command, current.recovery_recipe, current.codex_home, current.selected_account) == (
        item.command, item.recovery_recipe, None, None)
    assert not store.auth_root.exists()


def _process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def test_real_stale_socket_restore_and_still_live_rerun(store, tmp_path, monkeypatch):
    """Only this disposable socket/server is created and cleaned up."""
    root = tmp_path / "tm"
    root.mkdir(mode=0o700)
    socket_root = root / f"tmux-{os.geteuid()}"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "default"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.close()  # Same stale socket inode left by a disappeared owner.
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERMDECK_STATE_ROOT", raising=False)
    monkeypatch.delenv("TERMDECK_TMUX_RUNTIME", raising=False)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_TMUX_RUNTIME_BINDING", None)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", None)
    item = record(store, tmp_path, "disposable")
    backend = TmuxBackend(executable="/usr/bin/tmux")
    pid = start = None
    try:
        assert backend.reconciliation_snapshot([item])[item.id]["state"] == "missing"
        result = run(store, backend, tmp_path)
        assert result["counts"] == {"ready": 1}
        evidence = backend.reconciliation_snapshot([item])[item.id]
        assert evidence["state"] == "live"
        pid = evidence["server_pid"]
        start = _process_start(pid)
        pane = (evidence["pid"], evidence["start_time"])
        before = asdict(store.get(item.id))
        again = run(store, backend, tmp_path)
        assert again["counts"] == {"already-live": 1}
        assert asdict(store.get(item.id)) == before
        after = backend.reconciliation_snapshot([item])[item.id]
        assert (after["pid"], after["start_time"]) == pane
    finally:
        subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "kill-server"],
                       check=False, capture_output=True, timeout=3)
        if pid is not None:
            deadline = time.monotonic() + 3
            while _process_start(pid) == start and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _process_start(pid) != start, "owned disposable tmux generation survived cleanup"
