"""A failed stop must preserve the terminal's durable recovery state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from termdeck import cli, tmux_backend
from termdeck.history_control import BackendHistoryState, HistoryOwner
from termdeck.models import SessionRecord
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["tmux"], returncode, stdout, stderr)


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    database = tmp_path / "sessions.sqlite3"
    store = SessionStore(database)
    try:
        record = store.create("worker", "test", "worker --start", str(tmp_path))
        record = store.update(
            record.id,
            status="running",
            last_pid=12345,
            recovery_command="worker --resume saved-work",
            recovery_cwd=str(tmp_path),
            recovery_intent="active",
        )
        store.set_window_captain(record.window_id, record.id)
    finally:
        store.close()

    checkpoints = Mock()
    backend = TmuxBackend(executable="/usr/bin/tmux", checkpoint_manager=checkpoints)
    history = BackendHistoryState(
        owner=HistoryOwner.TMUX,
        depth=3,
        extent=100,
        command="worker",
        validated_at=time.monotonic(),
    )
    backend._scroll_views[record.id] = history
    # Even a prior UI query timeout cannot suppress the exit verification.
    backend._query_backoff_until = time.monotonic() + 60
    monkeypatch.setattr(backend, "require_available", lambda: None)
    monkeypatch.setattr(tmux_backend, "tmux_subprocess_environment", lambda **_kw: {})
    monkeypatch.setattr(cli, "TmuxBackend", lambda: backend)
    args = argparse.Namespace(
        db=database,
        selectors=[record.id],
        tag=None,
        all=False,
        dry_run=False,
        yes=True,
        include_pinned=False,
    )
    return SimpleNamespace(
        database=database,
        record=record,
        backend=backend,
        checkpoints=checkpoints,
        history=history,
        args=args,
    )


@pytest.mark.parametrize(
    ("stop", "observation"),
    [
        ("failure", "present"),
        ("timeout", "present"),
        ("failure", "refused"),
        ("failure", "timeout"),
    ],
)
def test_failed_cli_stop_preserves_recovery_and_captain(
    terminal, monkeypatch, capsys, stop, observation
):
    stopped = (
        subprocess.TimeoutExpired(["tmux", "kill-session"], 2)
        if stop == "timeout"
        else _completed(1, stderr="simulated stop failure")
    )
    observed = {
        "present": _completed(stdout=terminal.record.tmux_name + "\n"),
        "refused": _completed(1, stderr="permission denied"),
        "timeout": subprocess.TimeoutExpired(["tmux", "list-sessions"], 2),
    }[observation]
    run = Mock(side_effect=[stopped, observed])
    monkeypatch.setattr(tmux_backend.subprocess, "run", run)

    assert cli.cmd_kill(terminal.args) == 1

    result = json.loads(capsys.readouterr().out)
    assert result["sessions"][0]["ok"] is False
    assert "still present" in result["sessions"][0]["detail"] or "could not verify" in result["sessions"][0]["detail"]
    terminal.checkpoints.clear.assert_not_called()
    assert terminal.backend._scroll_views[terminal.record.id] is terminal.history
    store = SessionStore(terminal.database)
    try:
        assert store.get(terminal.record.id) == terminal.record
        assert store.captain_for_window(terminal.record.window_id).id == terminal.record.id
    finally:
        store.close()
    assert [call.args[0] for call in run.call_args_list] == [
        ["/usr/bin/tmux", "kill-session", "-t", "=" + terminal.record.tmux_name],
        ["/usr/bin/tmux", "list-sessions", "-F", "#{session_name}"],
    ]


@pytest.mark.parametrize(
    ("stop", "observation", "detail"),
    [
        ("success", None, "stopped tmux session"),
        ("failure", "other-session\n", "session already ended; marked stopped"),
        ("failure", None, "session already ended; marked stopped"),
        ("timeout", "other-session\n", "session already ended; marked stopped"),
    ],
)
def test_cli_stop_cleans_up_only_after_success_or_verified_exit(
    terminal, monkeypatch, capsys, stop, observation, detail
):
    if stop == "success":
        results = [_completed()]
    else:
        stopped = (
            subprocess.TimeoutExpired(["tmux", "kill-session"], 2)
            if stop == "timeout"
            else _completed(1, stderr="can't find session")
        )
        observed = (
            _completed(1, stderr="no server running on /disposable/socket")
            if observation is None
            else _completed(stdout=observation)
        )
        results = [stopped, observed]
    run = Mock(side_effect=results)
    monkeypatch.setattr(tmux_backend.subprocess, "run", run)

    assert cli.cmd_kill(terminal.args) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["sessions"][0]["ok"] is True
    assert result["sessions"][0]["detail"] == detail
    terminal.checkpoints.clear.assert_called_once_with(terminal.record)
    assert terminal.record.id not in terminal.backend._scroll_views
    store = SessionStore(terminal.database)
    try:
        updated = store.get(terminal.record.id)
        assert updated.status == "stopped"
        assert updated.visible is False
        assert updated.recovery_command is None
        assert updated.recovery_cwd is None
        assert updated.recovery_intent == "inactive"
        assert store.captain_for_window(terminal.record.window_id) is None
    finally:
        store.close()
    assert run.call_count == len(results)
    assert sum(call.args[0][1] == "kill-session" for call in run.call_args_list) == 1


@pytest.mark.skipif(not Path("/usr/bin/tmux").is_file(), reason="tmux unavailable")
def test_disposable_stop_preserves_similar_name_and_accepts_already_ended(
    tmp_path, monkeypatch
):
    # Keep the complete Unix socket path short and address only this server.
    with tempfile.TemporaryDirectory(prefix="td-stop-") as directory:
        runtime = Path(directory)
        socket_root = runtime / f"tmux-{os.geteuid()}"
        socket_root.mkdir(mode=0o700)
        socket = socket_root / "default"
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMUX_TMPDIR": str(runtime),
        }

        def run(*args, check=True):
            return subprocess.run(
                ["/usr/bin/tmux", "-S", str(socket), *args],
                env=environment,
                capture_output=True,
                text=True,
                check=check,
                timeout=5,
            )

        monkeypatch.setattr(
            tmux_backend, "tmux_subprocess_environment", lambda **_kw: environment
        )
        checkpoints = Mock()
        backend = TmuxBackend(executable="/usr/bin/tmux", checkpoint_manager=checkpoints)
        record = SessionRecord(
            id="disposable",
            name="stop target",
            tag="test",
            command="sleep 60",
            cwd=str(tmp_path),
            tmux_name="stop-target",
            created_at="now",
            last_opened_at="now",
            status="running",
        )
        try:
            run("-f", "/dev/null", "new-session", "-d", "-s", record.tmux_name, "sleep 60")
            run("new-session", "-d", "-s", record.tmux_name + "-keep", "sleep 60")
            before = run("display-message", "-p", "-t", "=stop-target-keep", "#{pane_pid}").stdout

            assert backend.terminate(record) is True
            assert backend.terminate(record) is False

            assert run("list-sessions", "-F", "#{session_name}").stdout == "stop-target-keep\n"
            assert run("display-message", "-p", "-t", "=stop-target-keep", "#{pane_pid}").stdout == before
            assert checkpoints.clear.call_count == 2
        finally:
            run("kill-server", check=False)
