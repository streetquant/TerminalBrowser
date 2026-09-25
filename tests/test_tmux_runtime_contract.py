"""Real disposable tmux coverage for routing across the launcher exec/exit."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest

from termdeck import storage_fabric as storage


@pytest.fixture
def runtime(monkeypatch):
    # Keep the complete Unix socket address below Linux's 108-byte limit.
    with tempfile.TemporaryDirectory(prefix="td-tmux-") as directory:
        root = Path(directory)
        device = f"{os.major(root.stat().st_dev)}:{os.minor(root.stat().st_dev)}"
        monkeypatch.setattr(storage, "_PRODUCTION_TMUX_RUNTIME_BINDING", (root, device))
        monkeypatch.setenv("TERMDECK_TMUX_RUNTIME", str(root))
        monkeypatch.setenv("TMUX_TMPDIR", str(root))
        monkeypatch.setenv("TMUX", "/tmp/foreign-tmux/default,123,0")
        try:
            yield root
        finally:
            # Only this fixture's server can be affected.
            subprocess.run(["/usr/bin/tmux", "-S", str(root / f"tmux-{os.geteuid()}" / "default"),
                            "kill-server"], capture_output=True, timeout=3)
            binding = storage._PRODUCTION_TMUX_RUNTIME_BINDING
            if isinstance(binding, storage._ProductionDirectoryBinding):
                binding.close()


def run_tmux(root, *args, check=True):
    environment = storage.tmux_subprocess_environment(materialize=True)
    return subprocess.run(["/usr/bin/tmux", *args], env=environment, capture_output=True,
                          text=True, timeout=5, check=check)


def test_runtime_survives_admitting_launcher_exit(runtime):
    script = """
import os, subprocess, sys
from pathlib import Path
from termdeck import storage_fabric as s
root=Path(sys.argv[1]); dev=root.stat().st_dev
s._PRODUCTION_TMUX_RUNTIME_BINDING=(root,f'{os.major(dev)}:{os.minor(dev)}')
env=s.tmux_subprocess_environment(materialize=True)
subprocess.run(['/usr/bin/tmux','-f','/dev/null','new-session','-d','-s','owned',
                '-c',str(root),'sleep 120'],env=env,check=True)
"""
    subprocess.run([sys.executable, "-c", script, str(runtime)], check=True, timeout=10)
    # The admitting process and every descriptor it owned are now gone.
    result = run_tmux(runtime, "display-message", "-p", "-t", "owned", "#{socket_path}")
    assert result.stdout.strip() == str(runtime / f"tmux-{os.geteuid()}" / "default")
    assert run_tmux(runtime, "list-sessions", "-F", "#{session_name}").stdout.strip() == "owned"
    assert run_tmux(runtime, "display-message", "-p", "-t", "owned", "#{pane_current_path}").stdout.strip() == str(runtime)
    assert "TMUX" not in storage.tmux_subprocess_environment(materialize=True)


def test_missing_socket_is_not_a_recovered_or_new_server(runtime):
    result = run_tmux(runtime, "list-sessions", check=False)
    assert result.returncode != 0
    assert not (runtime / f"tmux-{os.geteuid()}" / "default").exists()


def test_reconciliation_observes_real_admitted_server_and_preserves_other_session(runtime):
    from termdeck.session_store import SessionStore
    from termdeck.session_reconcile import reconcile_store
    from termdeck.tmux_backend import TmuxBackend

    store = SessionStore(runtime / "sessions.sqlite3")
    try:
        live = store.create("live", "test", "bash", str(runtime))
        missing = store.create("missing", "test", "bash", str(runtime))
        store.update(live.id, status="running", last_pid=99999999)
        store.update(missing.id, status="running", last_pid=os.getpid())
        run_tmux(runtime, "-f", "/dev/null", "new-session", "-d", "-s", live.tmux_name, "sleep 120")
        run_tmux(runtime, "new-session", "-d", "-s", "unrelated", "sleep 120")
        before = run_tmux(runtime, "display-message", "-p", "-t", "unrelated", "#{pane_pid}").stdout
        first = reconcile_store(store, backend=TmuxBackend(), hide_missing=False, recover_missing=True)
        states = {r.id: r for r in first.rows}
        assert states[live.id].observation == "live"
        assert states[live.id].live_process
        assert states[missing.id].after_status == "recovery-pending"
        assert not states[missing.id].live_process
        again = reconcile_store(store, backend=TmuxBackend(), hide_missing=False, recover_missing=True)
        assert again.changed == 0
        assert run_tmux(runtime, "display-message", "-p", "-t", "unrelated", "#{pane_pid}").stdout == before
    finally:
        store.close()


@pytest.mark.parametrize("target", ["root", "socket-parent", "socket"])
def test_unsafe_permissions_fail_without_chmod(runtime, target):
    path = runtime
    listener = None
    if target != "root":
        path = runtime / f"tmux-{os.geteuid()}"
        path.mkdir(mode=0o700)
    if target == "socket":
        path /= "default"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
    path.chmod(0o777)
    try:
        with pytest.raises(storage.StorageFabricError, match="permissions"):
            storage.tmux_subprocess_environment(materialize=True)
        assert path.stat().st_mode & 0o777 == 0o777
    finally:
        path.chmod(0o700)
        if listener:
            listener.close()


def test_foreign_socket_peer_is_rejected(runtime):
    parent = runtime / f"tmux-{os.geteuid()}"
    parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(parent / "default"))
        (parent / "default").chmod(0o600)
        listener.listen(2)
        with pytest.raises(storage.StorageFabricError, match="peer executable is foreign"):
            storage.tmux_subprocess_environment(materialize=True)


@pytest.mark.parametrize("target", ["socket-parent", "socket"])
def test_symlink_substitution_is_rejected(runtime, tmp_path, target):
    parent = runtime / f"tmux-{os.geteuid()}"
    if target == "socket-parent":
        parent.symlink_to(tmp_path, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
        (parent / "default").symlink_to(tmp_path / "foreign")
    with pytest.raises(storage.StorageFabricError, match="not a directory|not a Unix socket"):
        storage.tmux_subprocess_environment(materialize=True)
    assert not list(tmp_path.iterdir())


def test_runtime_owner_drift_is_rejected(runtime, monkeypatch):
    storage.tmux_subprocess_environment(materialize=True)
    monkeypatch.setattr(storage.os, "geteuid", lambda: runtime.stat().st_uid + 1)
    with pytest.raises(storage.StorageFabricError, match="owner"):
        storage.tmux_subprocess_environment(materialize=True)
