from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from termdeck.gtk_app import AttachTerminal, TermDeckWindow
from termdeck.tmux_backend import TmuxBackend


def test_attach_child_exit_schedules_one_restart() -> None:
    removed: list[int] = []
    scheduled: list[bool] = []
    attach = SimpleNamespace(
        _detached=False,
        process=object(),
        _attach_stable_source_id=17,
        _remove_glib_source=removed.append,
        _schedule_attach_restart=lambda: scheduled.append(True),
    )

    AttachTerminal._on_attach_child_exited(attach, None, 1)

    assert attach.process is None
    assert attach._attach_stable_source_id == 0
    assert removed == [17]
    assert scheduled == [True]


def test_attach_child_exit_is_ignored_after_detach() -> None:
    scheduled: list[bool] = []
    attach = SimpleNamespace(
        _detached=True,
        process=object(),
        _attach_stable_source_id=11,
        _remove_glib_source=lambda _source_id: None,
        _schedule_attach_restart=lambda: scheduled.append(True),
    )

    AttachTerminal._on_attach_child_exited(attach, None, 0)

    assert attach.process is not None
    assert scheduled == []


def test_attach_restart_uses_current_terminal_geometry() -> None:
    spawned: list[dict[str, int]] = []
    attach = SimpleNamespace(
        _attach_restart_source_id=42,
        _detached=False,
        process=SimpleNamespace(poll=lambda: 1),
        terminal=SimpleNamespace(
            get_column_count=lambda: 132,
            get_row_count=lambda: 43,
        ),
        _spawn=lambda **kwargs: spawned.append(kwargs),
        _schedule_attach_restart=lambda: None,
    )

    assert AttachTerminal._restart_attach_client(attach) is False

    assert attach._attach_restart_source_id == 0
    assert attach.process is None
    assert spawned == [{"columns": 132, "rows": 43}]


def test_attach_restart_failure_requeues() -> None:
    scheduled: list[bool] = []

    def fail_spawn(**_kwargs) -> None:
        raise OSError("pty unavailable")

    attach = SimpleNamespace(
        _attach_restart_source_id=9,
        _detached=False,
        process=None,
        terminal=SimpleNamespace(
            get_column_count=lambda: 0,
            get_row_count=lambda: 0,
        ),
        _spawn=fail_spawn,
        _schedule_attach_restart=lambda: scheduled.append(True),
    )

    assert AttachTerminal._restart_attach_client(attach) is False

    assert attach.process is None
    assert scheduled == [True]


def test_attach_restart_backoff_is_bounded_and_coalesced() -> None:
    attach = SimpleNamespace(
        _detached=False,
        _attach_restart_source_id=0,
        _attach_restart_attempts=0,
        _restart_attach_client=lambda: False,
    )
    with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=77) as timeout_add:
        AttachTerminal._schedule_attach_restart(attach)
        assert attach._attach_restart_source_id == 77
        assert attach._attach_restart_attempts == 1
        timeout_add.assert_called_once_with(150, attach._restart_attach_client)

        AttachTerminal._schedule_attach_restart(attach)
        assert timeout_add.call_count == 1

        attach._attach_restart_source_id = 0
        attach._attach_restart_attempts = 20
        AttachTerminal._schedule_attach_restart(attach)
        assert timeout_add.call_args.args[0] <= 5000


def test_attach_stable_period_resets_backoff() -> None:
    attach = SimpleNamespace(
        _attach_stable_source_id=91,
        _detached=False,
        process=SimpleNamespace(poll=lambda: None),
        _attach_restart_attempts=5,
    )

    assert AttachTerminal._mark_attach_stable(attach) is False
    assert attach._attach_stable_source_id == 0
    assert attach._attach_restart_attempts == 0


def test_recovery_metadata_survives_shell_fallback() -> None:
    record = SimpleNamespace(
        recovery_command="codex --yolo resume 019f9cd7-5cbb-7600-80c4-8cacdf88deb7",
        recovery_cwd="/8tb/bug-bounty/prompts",
    )

    assert TermDeckWindow._should_update_recovery_metadata(record, None, None) is False


def test_new_recovery_metadata_replaces_saved_resume_point() -> None:
    record = SimpleNamespace(
        recovery_command="codex --yolo resume old-session",
        recovery_cwd="/old",
    )

    assert (
        TermDeckWindow._should_update_recovery_metadata(
            record,
            "codex --yolo resume new-session",
            "/new",
        )
        is True
    )


def test_tmux_read_query_timeout_opens_control_plane_backoff() -> None:
    backend = TmuxBackend(query_timeout=0.05, failure_backoff=1.0)
    timeout = __import__("subprocess").TimeoutExpired(["tmux", "list-sessions"], 0.05)

    with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
        "termdeck.tmux_backend.subprocess.run", side_effect=timeout
    ) as run:
        first = backend._run(["list-sessions", "-F", "#{session_name}"], check=False, timeout=0.05)
        second = backend._run(["list-panes", "-a"], check=False, timeout=0.05)

    assert first.returncode == 124
    assert second.returncode == 124
    assert "backoff" in second.stderr
    assert run.call_count == 1


def test_tmux_mutation_is_not_suppressed_by_query_backoff() -> None:
    backend = TmuxBackend(query_timeout=0.05, failure_backoff=1.0)
    backend._query_backoff_until = __import__("time").monotonic() + 30.0
    completed = __import__("subprocess").CompletedProcess(["tmux", "send-keys"], 0, "", "")

    with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
        "termdeck.tmux_backend.subprocess.run", return_value=completed
    ) as run:
        result = backend._run(["send-keys", "-t", "session:0.0", "C-c"], check=False)

    assert result.returncode == 0
    run.assert_called_once()
