"""Durable copies survive terminal loss without mutating a live terminal."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from termdeck.session_durability import _atomic, backup_registry, main, save_screens
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend


class ScreenBackend:
    owner = {"state": "live", "pid": 200, "server_pid": 100, "start_time": 1}
    text = "important saved output\n"

    def reconciliation_snapshot(self, records):
        return {r.id: dict(self.owner) for r in records}

    def reconciliation_identity_current(self, owner):
        return owner == self.owner

    def pane_target(self, record):
        return record.tmux_name

    def _run(self, argv, **kwargs):
        assert argv[0] == "capture-pane"
        return SimpleNamespace(returncode=0, stdout=self.text)


@pytest.fixture
def saved(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    item = store.create("saved", "test", "/bin/bash", str(tmp_path))
    item = store.update(item.id, status="running", window_id="preserved-window", tab_index=7)
    yield store, item, tmp_path / "durability"
    store.close()


def test_missing_and_new_generation_keep_previous_screen(saved, capsys):
    store, item, root = saved
    backend = ScreenBackend()
    assert save_screens(store, backend, root) == {"saved": 1, "failed": 0}
    first = next((root / "screens" / item.id).glob("*.json"))
    before = first.read_bytes()
    backend.owner = {"state": "missing", "pid": None}
    assert save_screens(store, backend, root)["saved"] == 0
    assert first.read_bytes() == before
    backend.owner = {"state": "live", "pid": 201, "server_pid": 101, "start_time": 2}
    backend.text = "replacement shell\n"
    assert save_screens(store, backend, root)["saved"] == 1
    assert len(list(first.parent.glob("*.json"))) == 2
    assert first.read_bytes() == before
    assert main(["--root", str(root), "--session-id", item.id, "--generation", first.stem]) == 0
    assert capsys.readouterr().out == "important saved output\n"
    assert (first.stat().st_mode & 0o777) == 0o600
    assert (first.parent.stat().st_mode & 0o777) == 0o700


def test_capture_identity_race_does_not_publish(saved):
    store, _, root = saved
    backend = ScreenBackend()
    def capture(*_, **__):
        backend.owner = {"state": "missing", "pid": None}
        return SimpleNamespace(returncode=0, stdout="unbound output")
    backend._run = capture
    assert save_screens(store, backend, root) == {"saved": 0, "failed": 1}
    assert not list(root.rglob("*.json"))


def test_private_output_rejects_symlink_directory(saved, tmp_path):
    store, _, root = saved
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    root.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError, match="unsafe"):
        save_screens(store, ScreenBackend(), root)
    assert list(elsewhere.iterdir()) == []


def test_failed_replace_keeps_last_good_copy(tmp_path, monkeypatch):
    destination = tmp_path / "last-good"
    destination.write_bytes(b"last committed screen")
    def fail(*_):
        raise OSError("storage unavailable")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="storage unavailable"):
        _atomic(destination, b"new screen")
    assert destination.read_bytes() == b"last committed screen"
    assert list(tmp_path.iterdir()) == [destination]


def test_bounded_capture_pass_rotates_instead_of_starving_later_tabs(saved, monkeypatch):
    import termdeck.session_durability as durability
    store, item, root = saved
    second = store.create("second", "test", "/bin/bash", item.cwd)
    backend = ScreenBackend()
    clock = [0.0]
    monkeypatch.setattr(durability.time, "monotonic", lambda: clock[0])
    def slow_capture(*_, **__):
        clock[0] += 6
        return SimpleNamespace(returncode=0, stdout="bounded output")
    backend._run = slow_capture
    assert save_screens(store, backend, root) == {"saved": 1, "failed": 1}
    assert save_screens(store, backend, root) == {"saved": 1, "failed": 1}
    assert {p.parent.name for p in (root / "screens").glob("*/*.json")} == {item.id, second.id}


def test_registry_backup_includes_wal_and_preserves_layout(saved):
    store, item, root = saved
    store.conn.execute("PRAGMA wal_autocheckpoint=0")
    store.update(item.id, name="latest committed tab")
    path = backup_registry(store, root)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT name,window_id,tab_index FROM sessions WHERE id=?", (item.id,)).fetchone() == (
            "latest committed tab", "preserved-window", 7)
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.get(item.id).name == "latest committed tab"


def test_real_server_loss_preserves_output_and_automatically_recovers_same_boot(saved, tmp_path, monkeypatch):
    store, item, root = saved
    runtime = tmp_path / "tmux"
    runtime.mkdir(mode=0o700)
    for key in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(runtime))
    backend = TmuxBackend(pane_cache_ttl=0)
    # Each recovery attempt must have its own *exited* worker generation.
    script = """
import json,sys
from pathlib import Path
from termdeck.session_store import SessionStore
from termdeck.startup_recovery import recover_saved_terminals
from termdeck.tmux_backend import TmuxBackend
store=SessionStore(Path(sys.argv[1]),provision_auth_scopes=False)
try:
 print(json.dumps(recover_saved_terminals(store,TmuxBackend(pane_cache_ttl=0),runtime_root=Path(sys.argv[2]),readiness_timeout=5)))
finally: store.close()
"""
    def recover():
        result = subprocess.run([sys.executable, "-c", script, str(store.path), str(tmp_path / "attempts")],
                                check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    def tmux(*argv):
        return subprocess.check_output(["/usr/bin/tmux", *argv], text=True, timeout=5).strip()
    try:
        assert recover()["counts"] == {"ready": 1}
        first = backend.reconciliation_snapshot([item])[item.id]
        # Output fixture is placed in this test's private tmux buffer, never a user's terminal.
        tmux("set-buffer", "UNIQUE-DURABLE-SCREEN\n")
        tmux("send-keys", "-t", item.tmux_name, "-l", "printf 'UNIQUE-DURABLE-SCREEN\\n'")
        tmux("send-keys", "-t", item.tmux_name, "Enter")
        import time
        deadline = time.monotonic() + 3
        while "UNIQUE-DURABLE-SCREEN" not in backend.capture(item) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert save_screens(store, backend, root)["saved"] == 1
        screen = next((root / "screens" / item.id).glob("*.json"))
        assert "UNIQUE-DURABLE-SCREEN" in json.loads(screen.read_text())["text"]
        tmux("kill-server")  # Only TMUX_TMPDIR owned by this test.
        assert recover()["counts"] == {"ready": 1}
        second = backend.reconciliation_snapshot([item])[item.id]
        assert second["state"] == "live" and first["pid"] != second["pid"]
        assert recover()["counts"] == {"already-live": 1}
        assert backend.reconciliation_snapshot([item])[item.id] == second
        assert "UNIQUE-DURABLE-SCREEN" in json.loads(screen.read_text())["text"]
        assert store.get(item.id).window_id == "preserved-window"
    finally:
        subprocess.run(["/usr/bin/tmux", "kill-server"], capture_output=True, timeout=5)
