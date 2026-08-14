from __future__ import annotations

import threading
from contextlib import nullcontext
from types import MethodType, SimpleNamespace
from unittest import mock

from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
from termdeck.gtk_app import TermDeckApplication, TermDeckWindow
from termdeck.tmux_backend import AuthActivationResult, AuthTransactionResult


class ComboStub:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []
        self.active_id = None
        self.sensitive = None
        self.tooltip = ""

    def remove_all(self) -> None:
        self.rows.clear()

    def append(self, key: str, label: str) -> None:
        self.rows.append((key, label))

    def set_active_id(self, key: str) -> None:
        self.active_id = key

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = value

    def set_tooltip_text(self, value: str) -> None:
        self.tooltip = value

    def get_active_id(self) -> str | None:
        return self.active_id


def _state(
    *,
    selected: str | None = "alpha",
    canonical_binding_ready: bool = True,
) -> CodexAuthState:
    return CodexAuthState(
        codex_home="/private/codex",
        accounts=(
            CodexAccount("alpha", alias="alpha", active=selected == "alpha"),
            CodexAccount("beta", alias="beta", active=selected == "beta"),
        ),
        selected_account=selected,
        selected_account_key=selected,
        fingerprint=f"fingerprint-{selected}",
        codex_auth_available=True,
        canonical_binding_ready=canonical_binding_ready,
    )


def test_legacy_managed_terminal_shows_selectable_account_rows() -> None:
    combo = ComboStub()
    record = SimpleNamespace(
        id="legacy",
        external=False,
        pending_account=None,
        auth_restart_required=False,
    )
    window = SimpleNamespace(
        _auth_combo=combo,
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
    )

    TermDeckWindow._render_auth_selector(
        window,
        record,
        _state(),
        environment_ready=False,
    )

    assert combo.rows == [
        ("__none__", "Codex: choose account · upgrades terminal"),
        ("alpha", "alpha"),
        ("beta", "beta"),
    ]
    assert combo.active_id == "__none__"
    assert combo.sensitive is True
    assert "upgrade this managed terminal" in combo.tooltip


def test_stale_same_account_binding_is_rendered_as_a_reselectable_repair() -> None:
    combo = ComboStub()
    record = SimpleNamespace(
        id="legacy",
        external=False,
        pending_account=None,
        auth_restart_required=False,
    )
    window = SimpleNamespace(
        _auth_combo=combo,
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
    )

    TermDeckWindow._render_auth_selector(
        window,
        record,
        _state(canonical_binding_ready=False),
        environment_ready=True,
    )

    assert combo.rows == [
        ("__none__", "Codex: repair saved account binding"),
        ("alpha", "alpha · repair"),
        ("beta", "beta"),
    ]
    # Selecting alpha from this neutral row must emit Gtk's changed signal;
    # leaving alpha active would turn the required repair into a silent no-op.
    assert combo.active_id == "__none__"
    assert combo.sensitive is True
    assert "Reselect alpha" in combo.tooltip


def test_refresh_does_not_sync_observed_auth_while_switch_is_inflight() -> None:
    combo = ComboStub()
    record = SimpleNamespace(
        id="busy",
        external=False,
        selected_account="alpha",
        pending_account="beta",
        auth_restart_required=False,
    )
    updates = []

    class Store:
        def update(self, _session_id, **changes):
            updates.append(changes)
            raise AssertionError("in-flight refresh must not write SessionStore")

    window = SimpleNamespace(
        _auth_combo=combo,
        _auth_combo_updating=False,
        _auth_switch_inflight={record.id},
        _auth_environment_ready={},
        _auth_fingerprints={},
        app_context=SimpleNamespace(
            backend=SimpleNamespace(private_auth_environment_ready=lambda _record: True),
            codex_auth=SimpleNamespace(state=lambda _record: _state(selected="beta")),
            store=Store(),
        ),
        current_record=lambda: record,
    )
    window._render_auth_selector = MethodType(TermDeckWindow._render_auth_selector, window)

    assert TermDeckWindow._refresh_auth_selector(window) is False

    assert updates == []
    assert record.selected_account == "alpha"
    assert combo.sensitive is False
    assert combo.active_id == "beta"


def test_same_account_reselect_runs_the_safe_transaction_when_binding_is_stale() -> None:
    state = _state(canonical_binding_ready=False)
    record = SimpleNamespace(
        id="legacy",
        name="Legacy",
        external=False,
        selected_account="alpha",
        pending_account=None,
    )

    class Store:
        def update(self, session_id, **changes):
            assert session_id == record.id
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    combo = ComboStub()
    combo.active_id = "alpha"
    idle_calls = []
    transaction = AuthTransactionResult(
        True,
        switch_result=CodexSwitchResult(True, _state()),
        activation=AuthActivationResult(True, False),
    )
    backend = SimpleNamespace(private_auth_environment_ready=lambda _record: True)
    manager = SimpleNamespace(state=lambda _record: state)
    window = SimpleNamespace(
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
        _auth_switch_generation={},
        app_context=SimpleNamespace(backend=backend, codex_auth=manager, store=Store()),
        tabs={},
        current_record=lambda: record,
        show_toast=lambda *_args, **_kwargs: None,
        _refresh_auth_selector=lambda: None,
        _complete_auth_switch=lambda *_args: False,
    )

    with mock.patch("termdeck.gtk_app.auth_transaction_lock", return_value=nullcontext()), mock.patch(
        "termdeck.gtk_app.execute_auth_change", return_value=transaction
    ) as execute, mock.patch(
        "termdeck.gtk_app.threading.Thread", ImmediateThread
    ), mock.patch(
        "termdeck.gtk_app.GLib.idle_add",
        side_effect=lambda *args: idle_calls.append(args) or 73,
    ):
        TermDeckWindow._on_auth_account_changed(window, combo)

    execute.assert_called_once_with(backend, manager, record, state, "alpha")
    assert record.pending_account == "alpha"
    assert record.id in window._auth_switch_inflight
    assert idle_calls and idle_calls[0][1] == record.id


def test_thread_start_failure_restores_pending_state_and_clears_guard() -> None:
    state = _state(selected="alpha")
    record = SimpleNamespace(
        id="worker-failure",
        name="Worker failure",
        external=False,
        selected_account="alpha",
        pending_account="previous",
        auth_restart_required=False,
    )
    updates = []

    class Store:
        def update(self, session_id, **changes):
            assert session_id == record.id
            updates.append(changes)
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    combo = ComboStub()
    combo.active_id = "beta"
    refreshes = []
    messages = []
    tab = SimpleNamespace(record=record)
    window = SimpleNamespace(
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
        _auth_switch_generation={},
        app_context=SimpleNamespace(
            backend=SimpleNamespace(private_auth_environment_ready=lambda _record: True),
            codex_auth=SimpleNamespace(state=lambda _record: state),
            store=Store(),
        ),
        tabs={record.id: tab},
        current_record=lambda: record,
        show_toast=lambda message, **_kwargs: messages.append(message),
        _refresh_auth_selector=lambda: refreshes.append(True),
    )

    with mock.patch("termdeck.gtk_app.threading.Thread", FailingThread):
        TermDeckWindow._on_auth_account_changed(window, combo)

    assert updates == [
        {"pending_account": "beta"},
        {"pending_account": "previous"},
    ]
    assert record.pending_account == "previous"
    assert tab.record is record
    assert window._auth_switch_inflight == set()
    assert combo.sensitive is True
    assert refreshes == [True]
    assert any("could not start account switch worker" in message for message in messages)


def test_unusable_idle_id_falls_back_to_attached_main_context_source() -> None:
    initial_state = _state(selected="alpha")
    record = SimpleNamespace(
        id="idle-fallback",
        name="Idle fallback",
        external=False,
        selected_account="alpha",
        pending_account=None,
        auth_restart_required=False,
    )

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            assert session_id == record.id
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    lock_events = []

    class TransactionLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, *_args):
            lock_events.append("exit")

    class Source:
        callback = None
        args = ()

        def set_callback(self, callback, *args):
            self.callback = callback
            self.args = args

        def attach(self, _context):
            return 91

    source = Source()
    combo = ComboStub()
    combo.active_id = "beta"
    messages = []
    window = SimpleNamespace(
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
        _auth_switch_generation={},
        _auth_fingerprints={},
        _auth_environment_ready={},
        app_context=SimpleNamespace(
            backend=SimpleNamespace(private_auth_environment_ready=lambda _record: True),
            codex_auth=SimpleNamespace(state=lambda _record: initial_state),
            store=Store(),
        ),
        tabs={},
        current_record=lambda: record,
        show_toast=lambda message, **_kwargs: messages.append(message),
        _refresh_auth_selector=lambda: None,
    )
    window._persist_auth_transaction_result = MethodType(
        TermDeckWindow._persist_auth_transaction_result,
        window,
    )
    window._complete_auth_switch = MethodType(TermDeckWindow._complete_auth_switch, window)
    transaction = AuthTransactionResult(
        True,
        switch_result=CodexSwitchResult(True, _state(selected="beta")),
        activation=AuthActivationResult(True, False),
    )

    with mock.patch(
        "termdeck.gtk_app.auth_transaction_lock",
        return_value=TransactionLock(),
    ), mock.patch(
        "termdeck.gtk_app.execute_auth_change",
        return_value=transaction,
    ), mock.patch(
        "termdeck.gtk_app.threading.Thread",
        ImmediateThread,
    ), mock.patch(
        "termdeck.gtk_app.GLib.idle_add",
        return_value=0,
    ), mock.patch(
        "termdeck.gtk_app.GLib.idle_source_new",
        return_value=source,
    ):
        TermDeckWindow._on_auth_account_changed(window, combo)

    assert lock_events == ["enter"]
    assert window._auth_switch_inflight == {record.id}
    assert source.callback is not None

    assert source.callback(*source.args) is False

    assert lock_events == ["enter", "exit"]
    assert window._auth_switch_inflight == set()
    assert record.selected_account == "beta"
    assert record.pending_account is None


def test_failed_main_context_handoff_retains_lock_and_guard_until_poll(capsys) -> None:
    initial_state = _state(selected="alpha")
    record = SimpleNamespace(
        id="poll-fallback",
        name="Poll fallback",
        external=False,
        selected_account="alpha",
        pending_account=None,
        auth_restart_required=False,
    )

    class Store:
        def get(self, session_id):
            return record if session_id == record.id else None

        def update(self, session_id, **changes):
            assert session_id == record.id
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    lock_events = []

    class TransactionLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, *_args):
            lock_events.append("exit")

    destroyed = []

    class Source:
        def set_callback(self, _callback, *_args):
            pass

        def attach(self, _context):
            return 0

        def destroy(self):
            destroyed.append(True)

    combo = ComboStub()
    combo.active_id = "beta"
    messages = []
    window = SimpleNamespace(
        _auth_combo_updating=False,
        _auth_switch_inflight=set(),
        _auth_switch_generation={},
        _auth_fingerprints={},
        _auth_environment_ready={},
        _auth_completion_recovery_lock=threading.Lock(),
        _auth_completion_recoveries=[],
        app_context=SimpleNamespace(
            backend=SimpleNamespace(private_auth_environment_ready=lambda _record: True),
            codex_auth=SimpleNamespace(state=lambda _record: initial_state),
            store=Store(),
        ),
        tabs={},
        current_record=lambda: record,
        show_toast=lambda message, **_kwargs: messages.append(message),
        _refresh_auth_selector=lambda: None,
    )
    for method in (
        "_queue_auth_completion_recovery",
        "_drain_auth_completion_recoveries",
        "_persist_auth_transaction_result",
        "_complete_auth_switch",
    ):
        setattr(window, method, MethodType(getattr(TermDeckWindow, method), window))
    transaction = AuthTransactionResult(
        True,
        switch_result=CodexSwitchResult(True, _state(selected="beta")),
        activation=AuthActivationResult(True, False),
    )

    with mock.patch(
        "termdeck.gtk_app.auth_transaction_lock",
        return_value=TransactionLock(),
    ), mock.patch(
        "termdeck.gtk_app.execute_auth_change",
        return_value=transaction,
    ), mock.patch(
        "termdeck.gtk_app.threading.Thread",
        ImmediateThread,
    ), mock.patch(
        "termdeck.gtk_app.GLib.idle_add",
        return_value=0,
    ), mock.patch(
        "termdeck.gtk_app.GLib.idle_source_new",
        return_value=Source(),
    ):
        TermDeckWindow._on_auth_account_changed(window, combo)

    assert lock_events == ["enter"]
    assert destroyed == [True]
    assert window._auth_switch_inflight == {record.id}
    assert len(window._auth_completion_recoveries) == 1
    assert "retry on the auth poll" in capsys.readouterr().err

    TermDeckWindow._drain_auth_completion_recoveries(window)

    assert window._auth_completion_recoveries == []
    assert lock_events == ["enter", "exit"]
    assert window._auth_switch_inflight == set()
    assert record.selected_account == "beta"
    assert record.pending_account is None
    assert any("UI completion handoff failed" in message for message in messages)


def test_auth_completions_are_versioned_per_terminal() -> None:
    records = {
        "a": SimpleNamespace(id="a", name="A", selected_account=None),
        "b": SimpleNamespace(id="b", name="B", selected_account=None),
    }

    class Store:
        def get(self, session_id):
            return records.get(session_id)

        def update(self, session_id, **changes):
            current = records[session_id]
            records[session_id] = SimpleNamespace(**{**vars(current), **changes})
            return records[session_id]

    messages = []
    window = SimpleNamespace(
        _auth_switch_inflight={"a", "b"},
        _auth_switch_generation={"a": 1, "b": 1},
        _auth_fingerprints={},
        app_context=SimpleNamespace(store=Store()),
        tabs={},
        show_toast=lambda message, **kwargs: messages.append(message),
        _refresh_auth_selector=lambda: None,
    )
    window._persist_auth_transaction_result = MethodType(
        TermDeckWindow._persist_auth_transaction_result,
        window,
    )
    activation = AuthActivationResult(True, False)

    assert TermDeckWindow._complete_auth_switch(
        window,
        "a",
        1,
        CodexSwitchResult(True, _state(selected="alpha")),
        activation,
        "",
    ) is False
    assert TermDeckWindow._complete_auth_switch(
        window,
        "b",
        1,
        CodexSwitchResult(True, _state(selected="beta")),
        activation,
        "",
    ) is False

    assert records["a"].selected_account == "alpha"
    assert records["b"].selected_account == "beta"
    assert window._auth_switch_inflight == set()
    assert len(messages) == 2


def test_legacy_auth_completion_persists_replacement_and_clears_stale_recovery() -> None:
    record = SimpleNamespace(
        id="legacy",
        name="Legacy",
        selected_account=None,
        command="old-shell",
        cwd="/old",
        recovery_command="codex resume stale-uuid",
        recovery_cwd="/stale",
    )

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
        _auth_fingerprints={},
        app_context=SimpleNamespace(store=Store()),
        tabs={},
        show_toast=lambda *_args, **_kwargs: None,
        _refresh_auth_selector=lambda: None,
    )
    window._persist_auth_transaction_result = MethodType(
        TermDeckWindow._persist_auth_transaction_result,
        window,
    )
    activation = AuthActivationResult(
        True,
        True,
        pid=123,
        status="running",
        command="/bin/bash",
        cwd="/live/project",
        clear_recovery=True,
    )

    assert TermDeckWindow._complete_auth_switch(
        window,
        record.id,
        1,
        CodexSwitchResult(True, _state(selected="beta")),
        activation,
        "",
    ) is False

    assert record.command == "/bin/bash"
    assert record.cwd == "/live/project"
    assert record.recovery_command is None
    assert record.recovery_cwd is None
    assert record.status == "running"
    assert record.last_pid == 123


def test_inflight_auth_blocks_tab_hide_and_window_close() -> None:
    record = SimpleNamespace(id="busy", name="Busy", pinned=False)
    detached = []
    messages = []
    window = SimpleNamespace(
        _auth_switch_inflight={record.id},
        app_context=SimpleNamespace(
            store=SimpleNamespace(get=lambda session_id: record if session_id == record.id else None),
        ),
        tabs={record.id: SimpleNamespace(widget=SimpleNamespace(detach=lambda: detached.append(True)))},
        show_toast=lambda message, **_kwargs: messages.append(message),
    )

    assert TermDeckWindow.hide_tab(window, record.id, include_pinned=True) is False
    assert TermDeckWindow.on_delete(window) is True

    assert detached == []
    assert len(messages) == 2
    assert all("Codex account switch" in message for message in messages)


def test_inflight_auth_blocks_cross_window_move_before_store_mutation() -> None:
    record = SimpleNamespace(id="busy", name="Busy", window_id="source")
    messages = []
    source = SimpleNamespace(
        window_id="source",
        tabs={record.id: object()},
        _auth_switch_inflight={record.id},
        show_toast=lambda message, **_kwargs: messages.append(message),
    )
    destination = SimpleNamespace(window_id="destination")
    store = SimpleNamespace(get=lambda session_id: record if session_id == record.id else None)
    app = SimpleNamespace(
        windows=[source, destination],
        store=store,
        window_for_tab=lambda session_id: source if session_id == record.id else None,
    )

    assert TermDeckApplication.move_tab_to_window(app, record.id, destination) is None
    assert record.window_id == "source"
    assert messages == ["Wait for Busy's Codex account switch before moving this tab"]


def test_quit_is_deferred_while_any_auth_switch_is_inflight() -> None:
    messages = []
    quit_calls = []
    busy = SimpleNamespace(
        _auth_switch_inflight={"busy"},
        show_toast=lambda message, **_kwargs: messages.append(message),
    )
    app = SimpleNamespace(windows=[busy], quit=lambda: quit_calls.append(True))

    assert TermDeckApplication.quit_preserving_sessions(app) is False
    assert quit_calls == []
    assert messages == ["T1000 will stay open until the Codex account switch finishes"]
