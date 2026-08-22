from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck.gateway_models import GatewayModelStatus
from termdeck.gtk_app import TermDeckWindow
from termdeck.models import SessionRecord


SESSION_ID = "d2a7ed1b-d6fd-45de-8342-5f10cfed3c07"


class StyleStub:
    def __init__(self) -> None:
        self.classes: set[str] = set()

    def add_class(self, value: str) -> None:
        self.classes.add(value)

    def remove_class(self, value: str) -> None:
        self.classes.discard(value)


class ButtonStub:
    def __init__(self) -> None:
        self.visible = False
        self.sensitive = False
        self.tooltip = ""
        self.style = StyleStub()

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = bool(value)

    def set_tooltip_text(self, value: str) -> None:
        self.tooltip = value

    def get_style_context(self) -> StyleStub:
        return self.style


def _record(tmp_path: Path, *, record_id: str = "tab-models") -> SessionRecord:
    return SessionRecord(
        id=record_id,
        name="Models",
        tag="work",
        command="cc-go-ulw",
        cwd=str(tmp_path),
        tmux_name=f"termdeck_{record_id}",
        created_at="now",
        last_opened_at="now",
        status="running",
        recovery_command=f"cc-go-ulw --resume {SESSION_ID}",
        recovery_cwd=str(tmp_path),
    )


def test_stale_gateway_tab_exposes_enabled_refresh_control(tmp_path: Path) -> None:
    record = _record(tmp_path)
    button = ButtonStub()
    status = GatewayModelStatus(True, True, True, model_count=43)
    window = SimpleNamespace(
        _gateway_models_button=button,
        _gateway_model_probe_session_id=None,
        _gateway_model_probe_at=0.0,
        _gateway_model_status=None,
        _payload_restart_inflight=set(),
        _auth_switch_inflight=set(),
        current_record=lambda: record,
        app_context=SimpleNamespace(backend=SimpleNamespace()),
    )

    with mock.patch(
        "termdeck.gtk_app.process_start_epoch", return_value=1000.0
    ), mock.patch(
        "termdeck.gtk_app.classify_gateway_model_status", return_value=status
    ):
        assert TermDeckWindow._refresh_gateway_model_control(window, force=True) is False
    assert button.visible
    assert button.sensitive
    assert "43 gateway models" in button.tooltip
    assert "suggested-action" in button.style.classes


def test_fresh_gateway_tab_keeps_control_visible_but_disabled(tmp_path: Path) -> None:
    record = _record(tmp_path)
    button = ButtonStub()
    status = GatewayModelStatus(True, True, False, model_count=43)
    window = SimpleNamespace(
        _gateway_models_button=button,
        _gateway_model_probe_session_id=None,
        _gateway_model_probe_at=0.0,
        _gateway_model_status=None,
        _payload_restart_inflight=set(),
        _auth_switch_inflight=set(),
        current_record=lambda: record,
        app_context=SimpleNamespace(backend=SimpleNamespace()),
    )

    with mock.patch(
        "termdeck.gtk_app.process_start_epoch", return_value=3000.0
    ), mock.patch(
        "termdeck.gtk_app.classify_gateway_model_status", return_value=status
    ):
        TermDeckWindow._refresh_gateway_model_control(window, force=True)
    assert button.visible
    assert not button.sensitive
    assert "current" in button.tooltip


def test_refresh_click_requests_exact_same_tab_restart(tmp_path: Path) -> None:
    record = _record(tmp_path)
    calls = []
    messages = []
    window = SimpleNamespace(
        _gateway_model_status=GatewayModelStatus(True, True, True, model_count=43),
        current_record=lambda: record,
        _refresh_gateway_model_control=lambda force=False: calls.append(("refresh", force)),
        _start_managed_payload_restart=lambda rec, **kwargs: calls.append(
            ("restart", rec.id, kwargs)
        )
        or True,
        show_toast=lambda message, **kwargs: messages.append((message, kwargs)),
    )

    TermDeckWindow._on_gateway_models_refresh(window)

    assert calls == [
        ("refresh", True),
        (
            "restart",
            record.id,
            {"exact_live": True, "reason": "models", "model_count": 43},
        ),
        ("refresh", True),
    ]
    assert "43 models" in messages[-1][0]


def test_dead_managed_payload_uses_generic_recovery_path(tmp_path: Path) -> None:
    record = _record(tmp_path, record_id="dead")
    calls = []
    window = SimpleNamespace(
        _payload_restart_backoff_until={},
        _auth_switch_inflight=set(),
        _payload_restart_inflight=set(),
        app_context=SimpleNamespace(store=SimpleNamespace(get=lambda _sid: record)),
        _start_managed_payload_restart=lambda rec, **kwargs: calls.append(
            (rec.id, kwargs)
        )
        or True,
    )

    assert TermDeckWindow._on_live_payload_dead(window, record.id)
    assert calls == [
        (
            record.id,
            {"exact_live": False, "reason": "dead", "model_count": 0},
        )
    ]


def test_start_restart_defers_stream_before_worker_calls_backend(tmp_path: Path) -> None:
    record = _record(tmp_path)
    events = []
    completions = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self) -> None:
            self.target()

    widget = SimpleNamespace(
        defer_attach_for_target_replacement=lambda: events.append("defer") or True,
        resolve_deferred_attach=lambda: events.append("resolve"),
    )
    window = SimpleNamespace(
        _auth_switch_inflight=set(),
        _payload_restart_inflight=set(),
        _payload_restart_generation={},
        _auth_attach_protected=set(),
        _payload_restart_completion_lock=mock.MagicMock(),
        _payload_restart_completions=[],
        _refresh_gateway_model_control=lambda force=False: None,
        _complete_managed_payload_restart=lambda *_args: False,
        tabs={record.id: SimpleNamespace(widget=widget)},
        app_context=SimpleNamespace(
            backend=SimpleNamespace(restart_exact_payload=lambda _record: events.append("backend") or 777)
        ),
        show_toast=lambda *_args, **_kwargs: None,
    )

    with mock.patch("termdeck.gtk_app.threading.Thread", ImmediateThread), mock.patch(
        "termdeck.gtk_app._schedule_glib_callback",
        side_effect=lambda callback, *args, **_kwargs: completions.append(
            (callback, args)
        )
        or (True, ""),
    ):
        assert TermDeckWindow._start_managed_payload_restart(
            window,
            record,
            exact_live=True,
            reason="models",
            model_count=43,
        )

    assert events == ["defer", "backend"]
    assert window._payload_restart_inflight == {record.id}
    assert window._auth_attach_protected == {record.id}
    assert completions[0][1] == (
        record.id,
        1,
        777,
        "",
        "models",
        43,
        "restarted",
        None,
        None,
        False,
    )


def test_successful_restart_persists_pid_and_resolves_protected_attach(tmp_path: Path) -> None:
    record = _record(tmp_path)
    updated = SessionRecord(**{**record.__dict__, "last_pid": 777})
    updates = []
    resolved = []
    messages = []
    window = SimpleNamespace(
        _payload_restart_generation={record.id: 2},
        _payload_restart_inflight={record.id},
        _payload_restart_backoff_until={record.id: 999.0},
        _auth_attach_protected=set(),
        tabs={record.id: SimpleNamespace(record=record, widget=object())},
        app_context=SimpleNamespace(
            store=SimpleNamespace(
                get=lambda _sid: record,
                update=lambda _sid, **changes: updates.append(changes) or updated,
            )
        ),
        _resolve_auth_attach_gap=lambda sid: resolved.append(sid),
        _refresh_gateway_model_control=lambda force=False: None,
        show_toast=lambda message, **kwargs: messages.append((message, kwargs)),
        side=SimpleNamespace(refresh=lambda: None),
    )

    assert not TermDeckWindow._complete_managed_payload_restart(
        window,
        record.id,
        2,
        777,
        "",
        "models",
        43,
    )
    assert record.id not in window._payload_restart_inflight
    assert window._auth_attach_protected == {record.id}
    assert resolved == [record.id]
    assert updates[0]["last_pid"] == 777
    assert updates[0]["status"] == "running"
    assert "43 gateway models" in messages[-1][0]


def test_failed_restart_sets_backoff_and_reconnects_once(tmp_path: Path) -> None:
    record = _record(tmp_path)
    updates = []
    resolved = []
    window = SimpleNamespace(
        _payload_restart_generation={record.id: 1},
        _payload_restart_inflight={record.id},
        _payload_restart_backoff_until={},
        _auth_attach_protected={record.id},
        tabs={record.id: SimpleNamespace(record=record, widget=object())},
        app_context=SimpleNamespace(
            store=SimpleNamespace(
                get=lambda _sid: record,
                update=lambda _sid, **changes: updates.append(changes) or record,
            )
        ),
        _resolve_auth_attach_gap=lambda sid: resolved.append(sid),
        _refresh_gateway_model_control=lambda force=False: None,
        show_toast=lambda *_args, **_kwargs: None,
        side=SimpleNamespace(refresh=lambda: None),
    )

    TermDeckWindow._complete_managed_payload_restart(
        window,
        record.id,
        1,
        None,
        "boom",
        "dead",
        0,
    )
    assert record.id not in window._auth_attach_protected
    assert window._payload_restart_backoff_until[record.id] > 0
    assert updates[0]["status"] == "recovery-pending"
    assert updates[0]["recovery_last_error"] == "boom"
    assert resolved == [record.id]


def test_shell_bridge_completion_rewrites_only_current_payload_recipe(tmp_path: Path) -> None:
    record = _record(tmp_path)
    updates = []
    resolved = []
    messages = []
    shell = SessionRecord(
        **{
            **record.__dict__,
            "command": "/usr/bin/bash -i",
            "cwd": str(tmp_path),
            "recovery_command": None,
            "recovery_cwd": None,
            "last_pid": 888,
        }
    )
    window = SimpleNamespace(
        _payload_restart_generation={record.id: 3},
        _payload_restart_inflight={record.id},
        _payload_restart_backoff_until={},
        _auth_attach_protected=set(),
        tabs={record.id: SimpleNamespace(record=record, widget=object())},
        app_context=SimpleNamespace(
            store=SimpleNamespace(
                get=lambda _sid: record,
                update=lambda _sid, **changes: updates.append(changes) or shell,
            )
        ),
        _resolve_auth_attach_gap=lambda sid: resolved.append(sid),
        _refresh_gateway_model_control=lambda force=False: None,
        show_toast=lambda message, **kwargs: messages.append((message, kwargs)),
        side=SimpleNamespace(refresh=lambda: None),
    )

    TermDeckWindow._complete_managed_payload_restart(
        window,
        record.id,
        3,
        888,
        "",
        "dead",
        0,
        "shell",
        "/usr/bin/bash -i",
        str(tmp_path),
        True,
    )

    change = updates[0]
    assert change["command"] == "/usr/bin/bash -i"
    assert change["cwd"] == str(tmp_path)
    assert change["recovery_command"] is None
    assert change["recovery_cwd"] is None
    assert change["restart_policy"] == "on-failure"
    assert window.tabs[record.id].record is shell
    assert resolved == [record.id]
    assert "interactive shell" in messages[-1][0]
