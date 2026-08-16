from __future__ import annotations

import threading
from types import MethodType, SimpleNamespace
from unittest import mock

from termdeck.gtk_app import AttachTerminal, TermDeckWindow
from termdeck.tmux_backend import TmuxBackend
from termdeck.tmux_control import ClientState


def test_stream_reconnect_clears_stable_timer_and_classifies_once() -> None:
    removed: list[int] = []
    classified: list[bool] = []
    attach = SimpleNamespace(
        _detached=False,
        _stream_generation=3,
        _attach_stable_source_id=17,
        _remove_glib_source=removed.append,
        _classify_stream_interruption=lambda: classified.append(True),
    )

    assert AttachTerminal._apply_stream_state(attach, 3, ClientState.RECONNECTING) is False

    assert attach._attach_stable_source_id == 0
    assert removed == [17]
    assert classified == [True]


def test_missing_attach_target_stops_every_restart_path() -> None:
    torn: list[bool] = []
    attach = SimpleNamespace(
        _detached=False,
        _terminally_stopped=False,
        _attach_restart_deferred=False,
        _attach_exit_handler=lambda: False,
        _teardown_stream=lambda: torn.append(True),
    )

    AttachTerminal._classify_stream_interruption(attach)

    assert attach._terminally_stopped is True
    assert attach._attach_restart_deferred is False
    assert torn == [True]

    # A stopped widget must never schedule a new control-mode connect, even
    # when GTK keeps delivering size allocations.
    attach.controller = None
    attach.terminal = SimpleNamespace(
        get_column_count=lambda: 132,
        get_row_count=lambda: 43,
    )
    with mock.patch("termdeck.gtk_app.GLib.timeout_add") as timeout_add:
        AttachTerminal._on_terminal_size_allocate(attach, None, None)
    timeout_add.assert_not_called()


def _auth_gap_fixture(*, target_live: bool):
    record = SimpleNamespace(id="auth-gap", name="Auth gap", pending_account="beta")
    backend_checks: list[str] = []
    store_updates: list[dict[str, object]] = []
    scheduled: list[bool] = []
    removed_sources: list[int] = []

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            assert session_id == record.id
            store_updates.append(changes)
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class Backend:
        def session_exists(self, checked_record):
            backend_checks.append(checked_record.id)
            return target_live

    attach = SimpleNamespace(
        _detached=False,
        _terminally_stopped=False,
        _attach_restart_deferred=False,
        _stream_generation=1,
        _attach_stable_source_id=17,
        controller=None,
        terminal=SimpleNamespace(
            get_column_count=lambda: 132,
            get_row_count=lambda: 43,
        ),
        _remove_glib_source=removed_sources.append,
        _connect_stream=lambda **_kwargs: scheduled.append(True),
        _teardown_stream=lambda: removed_sources.append(99),
        _cancel_pending_connect=lambda: None,
    )
    attach._classify_stream_interruption = (
        lambda: AttachTerminal._classify_stream_interruption(attach)
    )
    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        _auth_fingerprints={},
        app_context=SimpleNamespace(store=Store(), backend=Backend()),
        tabs={record.id: SimpleNamespace(widget=attach)},
        show_toast=lambda *_args, **_kwargs: None,
        _refresh_auth_selector=lambda: None,
    )
    window._finalize_missing_attach_target = lambda session_id: False
    window._attach_client_should_retry = lambda: TermDeckWindow._attach_client_should_retry(
        window,
        record.id,
    )
    attach._attach_exit_handler = window._attach_client_should_retry
    attach.resolve_deferred_attach = lambda: AttachTerminal.resolve_deferred_attach(attach)
    return window, attach, record, backend_checks, store_updates, scheduled, removed_sources


def test_auth_switch_gap_defers_attach_and_completion_resumes_once() -> None:
    window, attach, record, backend_checks, store_updates, scheduled, _removed = (
        _auth_gap_fixture(target_live=True)
    )

    AttachTerminal._apply_stream_state(attach, 1, ClientState.RECONNECTING)

    assert attach._attach_restart_deferred is True
    assert backend_checks == []
    assert scheduled == []
    with mock.patch("termdeck.gtk_app.GLib.timeout_add") as timeout_add:
        AttachTerminal._on_terminal_size_allocate(attach, None, None)
    timeout_add.assert_not_called()
    assert scheduled == []

    assert TermDeckWindow._complete_auth_switch(
        window,
        record.id,
        1,
        None,
        None,
        "account switch failed",
    ) is False

    assert window._auth_switch_inflight == set()
    assert store_updates == [{"pending_account": None}]
    assert backend_checks == [record.id]
    assert attach._attach_restart_deferred is False
    assert attach._terminally_stopped is False
    assert scheduled == [True]
    # The guard ends only after the replacement attach has stayed alive; a
    # second client exit before then is still attributable to this switch.
    assert window._auth_attach_protected == {record.id}
    TermDeckWindow._on_auth_attach_stable(window, record.id)
    assert window._auth_attach_protected == set()

    TermDeckWindow._resolve_auth_attach_gap(window, record.id)
    assert backend_checks == [record.id]
    assert scheduled == [True]


def test_auth_switch_gap_completion_keeps_missing_target_tab_protected() -> None:
    window, attach, record, backend_checks, _updates, scheduled, removed = (
        _auth_gap_fixture(target_live=False)
    )

    AttachTerminal._apply_stream_state(attach, 1, ClientState.RECONNECTING)
    assert attach._attach_restart_deferred is True
    assert backend_checks == []
    assert scheduled == []

    with mock.patch("termdeck.gtk_app.GLib.idle_add", return_value=88) as idle_add:
        assert TermDeckWindow._complete_auth_switch(
            window,
            record.id,
            1,
            None,
            None,
            "account switch failed",
        ) is False

    assert backend_checks == [record.id]
    idle_add.assert_not_called()
    assert window._auth_attach_protected == {record.id}
    assert attach._attach_restart_deferred is True
    assert attach._terminally_stopped is False
    assert scheduled == []
    # The stream was torn down exactly once when the interruption deferred;
    # completion with a dead target must not tear it down again.
    assert removed == [17, 99]

    # A delayed replacement remains eligible for in-place reattach instead of
    # being hidden from its notebook/window as a missing session.
    window.app_context.backend.session_exists = lambda _record: True
    TermDeckWindow._resolve_auth_attach_gap(window, record.id)
    assert attach._attach_restart_deferred is False
    assert scheduled == [True]
    assert window._auth_attach_protected == {record.id}


def test_current_auth_completion_with_missing_record_resolves_deferred_attach() -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap")
    resolved: list[bool] = []
    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        app_context=SimpleNamespace(store=SimpleNamespace(get=lambda _session_id: None)),
        tabs={
            record.id: SimpleNamespace(
                widget=SimpleNamespace(resolve_deferred_attach=lambda: resolved.append(True)),
            ),
        },
        _refresh_auth_selector=lambda: None,
    )

    assert TermDeckWindow._complete_auth_switch(
        window,
        record.id,
        1,
        None,
        None,
        "missing completion",
    ) is False

    assert window._auth_switch_inflight == set()
    assert resolved == [True]


def test_stale_auth_completion_releases_only_its_lock() -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap")
    resolved: list[bool] = []
    refreshed: list[bool] = []
    exits: list[bool] = []

    class TransactionLock:
        def __exit__(self, *_args):
            exits.append(True)

    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 2},
        app_context=SimpleNamespace(
            store=SimpleNamespace(
                get=lambda _session_id: (_ for _ in ()).throw(
                    AssertionError("stale completion must not read durable state")
                ),
            ),
        ),
        tabs={
            record.id: SimpleNamespace(
                widget=SimpleNamespace(resolve_deferred_attach=lambda: resolved.append(True)),
            ),
        },
        _refresh_auth_selector=lambda: refreshed.append(True),
    )

    assert TermDeckWindow._complete_auth_switch(
        window,
        record.id,
        1,
        None,
        None,
        "stale completion",
        TransactionLock(),
    ) is False

    assert exits == [True]
    assert window._auth_switch_inflight == {record.id}
    assert refreshed == []
    assert resolved == []


def test_recovery_drain_does_not_readd_guard_for_stale_generation() -> None:
    exits: list[bool] = []

    class TransactionLock:
        def __exit__(self, *_args):
            exits.append(True)

    payload = ("auth-gap", 1, None, None, "stale completion", TransactionLock(), 0)
    window = SimpleNamespace(
        _auth_switch_inflight=set(),
        _auth_switch_generation={"auth-gap": 2},
        _auth_completion_recovery_lock=threading.Lock(),
        _auth_completion_recoveries=[payload],
    )
    window._complete_auth_switch = MethodType(TermDeckWindow._complete_auth_switch, window)

    TermDeckWindow._drain_auth_completion_recoveries(window)

    assert exits == [True]
    assert window._auth_completion_recoveries == []
    assert window._auth_switch_inflight == set()


def test_recovery_drain_isolates_one_callback_failure(capsys) -> None:
    calls: list[str] = []

    def complete(session_id, *_args):
        calls.append(session_id)
        if session_id == "first":
            raise RuntimeError("broken callback")
        return False

    window = SimpleNamespace(
        _auth_completion_recovery_lock=threading.Lock(),
        _auth_completion_recoveries=[
            ("first", 1, None, None, "", None, 0),
            ("second", 1, None, None, "", None, 0),
        ],
        _complete_auth_switch=complete,
    )

    TermDeckWindow._drain_auth_completion_recoveries(window)

    assert calls == ["first", "second"]
    assert window._auth_completion_recoveries == []
    assert "auth completion recovery failed" in capsys.readouterr().err


def test_retry_status_failure_cannot_drop_poll_drained_completion(capsys) -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap", pending_account="beta")
    exits: list[bool] = []
    retries: list[tuple[object, tuple[object, ...]]] = []
    update_attempts = 0
    toast_attempts = 0

    def show_toast(*_args, **_kwargs) -> None:
        nonlocal toast_attempts
        toast_attempts += 1
        if toast_attempts == 1:
            raise RuntimeError("toast scheduling failed")

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            nonlocal update_attempts
            assert session_id == record.id
            update_attempts += 1
            if update_attempts == 1:
                raise OSError("database temporarily busy")
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class TransactionLock:
        def __exit__(self, *_args):
            exits.append(True)

    lock = TransactionLock()
    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        _auth_completion_recovery_lock=threading.Lock(),
        _auth_completion_recoveries=[
            (record.id, 1, None, None, "account switch failed", lock, 0),
        ],
        app_context=SimpleNamespace(store=Store()),
        tabs={},
        show_toast=show_toast,
        _refresh_auth_selector=lambda: None,
    )
    window._complete_auth_switch = MethodType(TermDeckWindow._complete_auth_switch, window)

    with mock.patch(
        "termdeck.gtk_app.GLib.timeout_add",
        side_effect=lambda _delay, callback, *args: retries.append((callback, args)) or 73,
    ):
        TermDeckWindow._drain_auth_completion_recoveries(window)

    assert update_attempts == 1
    assert len(retries) == 1
    assert exits == []
    assert window._auth_switch_inflight == {record.id}
    assert window._auth_completion_recoveries == []
    assert "could not show auth persistence retry status" in capsys.readouterr().err

    callback, args = retries[0]
    assert callback(*args) is False
    assert update_attempts == 2
    assert exits == [True]
    assert window._auth_switch_inflight == set()
    assert record.pending_account is None


def test_persistence_failure_keeps_lock_guard_and_attach_until_retry_commits() -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap", pending_account="beta")
    resolved: list[bool] = []
    refreshed: list[bool] = []
    exits: list[bool] = []
    messages: list[str] = []
    update_attempts = 0

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            nonlocal update_attempts
            assert session_id == record.id
            update_attempts += 1
            if update_attempts <= 2:
                raise OSError("database temporarily busy")
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class TransactionLock:
        def __exit__(self, *_args):
            exits.append(True)

    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        app_context=SimpleNamespace(store=Store()),
        tabs={
            record.id: SimpleNamespace(
                widget=SimpleNamespace(resolve_deferred_attach=lambda: resolved.append(True)),
            ),
        },
        show_toast=lambda message, **_kwargs: messages.append(message),
        _refresh_auth_selector=lambda: refreshed.append(True),
    )
    window._complete_auth_switch = MethodType(TermDeckWindow._complete_auth_switch, window)
    lock = TransactionLock()
    retry = []

    with mock.patch(
        "termdeck.gtk_app.GLib.timeout_add",
        side_effect=lambda delay, callback, *args: retry.append((delay, callback, args)) or 73,
    ) as timeout_add:
        assert TermDeckWindow._complete_auth_switch(
            window,
            record.id,
            1,
            None,
            None,
            "account switch failed",
            lock,
        ) is False

        assert update_attempts == 1
        assert exits == []
        assert window._auth_switch_inflight == {record.id}
        assert refreshed == []
        assert resolved == []
        assert retry and retry[0][0] == 500
        assert "remains protected" in messages[0]

        _delay, callback, args = retry[0]
        assert callback(*args) is False

        assert update_attempts == 2
        assert exits == []
        assert window._auth_switch_inflight == {record.id}
        assert [entry[0] for entry in retry] == [500, 1000]

        _delay, callback, args = retry[1]
        assert callback(*args) is False

    assert timeout_add.call_count == 2
    assert update_attempts == 3
    assert exits == [True]
    assert window._auth_switch_inflight == set()
    assert record.pending_account is None
    assert refreshed == [True]
    assert resolved == [True]


def test_unavailable_retry_timer_stays_guarded_for_existing_auth_poll() -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap", pending_account="beta")
    exits: list[bool] = []
    messages: list[str] = []
    destroyed: list[bool] = []

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, _session_id, **_changes):
            raise OSError("database unavailable")

    class TransactionLock:
        def __exit__(self, *_args):
            exits.append(True)

    class Source:
        def set_callback(self, _callback, *_args):
            pass

        def attach(self, _context):
            return 0

        def destroy(self):
            destroyed.append(True)

    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        _auth_completion_recovery_lock=threading.Lock(),
        _auth_completion_recoveries=[],
        app_context=SimpleNamespace(store=Store()),
        tabs={},
        show_toast=lambda message, **_kwargs: messages.append(message),
        _refresh_auth_selector=lambda: None,
    )
    window._queue_auth_completion_recovery = MethodType(
        TermDeckWindow._queue_auth_completion_recovery,
        window,
    )
    window._complete_auth_switch = MethodType(TermDeckWindow._complete_auth_switch, window)
    lock = TransactionLock()

    with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=0) as timeout_add, mock.patch(
        "termdeck.gtk_app.GLib.timeout_source_new",
        return_value=Source(),
    ):
        assert TermDeckWindow._complete_auth_switch(
            window,
            record.id,
            1,
            None,
            None,
            "account switch failed",
            lock,
        ) is False

    timeout_add.assert_called_once()
    assert destroyed == [True]
    assert exits == []
    assert window._auth_switch_inflight == {record.id}
    assert len(window._auth_completion_recoveries) == 1
    assert window._auth_completion_recoveries[0][5] is lock
    assert any("remains protected" in message for message in messages)
    assert any("next auth poll" in message for message in messages)


def test_successful_commit_resolves_attach_even_if_selector_refresh_fails(capsys) -> None:
    record = SimpleNamespace(id="auth-gap", name="Auth gap", pending_account="beta")
    resolved: list[bool] = []

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            assert session_id == record.id
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        _auth_switch_generation={record.id: 1},
        app_context=SimpleNamespace(store=Store()),
        tabs={
            record.id: SimpleNamespace(
                widget=SimpleNamespace(resolve_deferred_attach=lambda: resolved.append(True)),
            ),
        },
        show_toast=lambda *_args, **_kwargs: None,
        _refresh_auth_selector=lambda: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    assert TermDeckWindow._complete_auth_switch(
        window,
        record.id,
        1,
        None,
        None,
        "account switch failed",
    ) is False

    assert window._auth_switch_inflight == set()
    assert resolved == [True]
    assert "could not refresh auth selector" in capsys.readouterr().err


def test_stream_state_change_is_ignored_after_detach() -> None:
    classified: list[bool] = []
    attach = SimpleNamespace(
        _detached=True,
        _stream_generation=1,
        _classify_stream_interruption=lambda: classified.append(True),
    )

    assert AttachTerminal._apply_stream_state(attach, 1, ClientState.RECONNECTING) is False

    assert classified == []


def test_resolve_deferred_attach_uses_current_terminal_geometry() -> None:
    connected: list[dict[str, int]] = []
    attach = SimpleNamespace(
        _detached=False,
        _terminally_stopped=False,
        _attach_restart_deferred=True,
        _attach_exit_handler=lambda: True,
        terminal=SimpleNamespace(
            get_column_count=lambda: 132,
            get_row_count=lambda: 43,
        ),
        _connect_stream=lambda **kwargs: connected.append(kwargs),
    )

    assert AttachTerminal.resolve_deferred_attach(attach) is False

    assert attach._attach_restart_deferred is False
    assert connected == [{"columns": 132, "rows": 43}]


def test_terminally_stopped_attach_cannot_be_resumed() -> None:
    connected: list[dict[str, int]] = []
    attach = SimpleNamespace(
        _detached=False,
        _terminally_stopped=True,
        _attach_restart_deferred=True,
        terminal=SimpleNamespace(
            get_column_count=lambda: 132,
            get_row_count=lambda: 43,
        ),
        _connect_stream=lambda **kwargs: connected.append(kwargs),
    )

    assert AttachTerminal.resolve_deferred_attach(attach) is False
    assert connected == []


def test_missing_attach_makes_stopped_legacy_shell_reopenable() -> None:
    record = SimpleNamespace(
        id="legacy",
        name="Legacy shell",
        external=False,
        status="stopped",
        visible=True,
        last_pid=414,
        recovery_command=None,
        recovery_cwd=None,
    )
    updates: list[dict[str, object]] = []
    removed: list[str] = []
    toasts: list[tuple[str, str]] = []

    class Store:
        def get(self, _session_id):
            return record

        def update(self, _session_id, **changes):
            updates.append(changes)
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    window = SimpleNamespace(
        app_context=SimpleNamespace(store=Store()),
        _remove_tab_widget=removed.append,
        _renumber_visible_tabs=lambda: None,
        side=SimpleNamespace(refresh=lambda: None),
        master_window=None,
        show_toast=lambda message, kind: toasts.append((message, kind)),
    )

    closed_at = "2026-08-12T20:00:00.123456+00:00"
    with mock.patch("termdeck.gtk_app.utc_now", return_value=closed_at):
        assert TermDeckWindow._finalize_missing_attach_target(window, record.id) is False

    assert updates == [
        {
            "status": "missing",
            "visible": False,
            "last_pid": None,
            "last_opened_at": closed_at,
        }
    ]
    assert record.status == "missing"
    assert removed == [record.id]
    assert "managed shell" in toasts[0][0]

    # A duplicate idle callback must not turn the replaceable marker into an
    # exact-recovery obligation that has no recovery metadata.
    with mock.patch("termdeck.gtk_app.utc_now", return_value=closed_at):
        assert TermDeckWindow._finalize_missing_attach_target(window, record.id) is False
    assert record.status == "missing"
    assert updates[-1]["status"] == "missing"


def test_teardown_stream_stops_controller_and_bumps_generation() -> None:
    stops: list[bool] = []
    attach = SimpleNamespace(
        controller=SimpleNamespace(stop=lambda: stops.append(True)),
        _stream_generation=4,
        _output_lock=threading.Lock(),
        _output_buffer=bytearray(b"stale"),
        _output_drain_scheduled=True,
    )

    AttachTerminal._teardown_stream(attach)

    assert attach.controller is None
    assert stops == [True]
    assert attach._stream_generation == 5
    assert bytes(attach._output_buffer) == b""
    assert attach._output_drain_scheduled is False


def test_stale_stream_output_is_discarded_after_teardown() -> None:
    fed: list[bytes] = []
    attach = SimpleNamespace(
        _detached=False,
        _stream_generation=2,
        _output_lock=threading.Lock(),
        _output_buffer=bytearray(b"stale"),
        _output_drain_scheduled=True,
        terminal=SimpleNamespace(feed=fed.append),
    )

    # A drain queued by the previous controller generation must clear its
    # buffered bytes without painting them into the live emulator.
    assert AttachTerminal._drain_stream_output(attach, 1) is False

    assert fed == []
    assert bytes(attach._output_buffer) == b""
    assert attach._output_drain_scheduled is False


def test_attach_stable_notifies_handler_only_while_connected() -> None:
    notified: list[bool] = []
    attach = SimpleNamespace(
        _attach_stable_source_id=91,
        _detached=False,
        controller=SimpleNamespace(state=ClientState.CONNECTED),
        _attach_stable_handler=lambda: notified.append(True),
    )

    assert AttachTerminal._mark_attach_stable(attach) is False
    assert attach._attach_stable_source_id == 0
    assert notified == [True]

    disconnected = SimpleNamespace(
        _attach_stable_source_id=7,
        _detached=False,
        controller=None,
        _attach_stable_handler=lambda: notified.append(True),
    )

    assert AttachTerminal._mark_attach_stable(disconnected) is False
    assert disconnected._attach_stable_source_id == 0
    assert notified == [True]


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
