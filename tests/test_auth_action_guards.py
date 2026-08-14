from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from termdeck.gtk_app import Gtk, MasterWindow, SidePane, TermDeckApplication, TermDeckWindow


def _record(session_id: str):
    return SimpleNamespace(
        id=session_id,
        name=session_id.title(),
        window_id="main",
    )


class _AcceptedDialog:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def format_secondary_text(self, _text: str) -> None:
        pass

    def run(self):
        return Gtk.ResponseType.OK

    def destroy(self) -> None:
        pass


def test_terminate_records_skips_auth_switching_tab_and_reports_actual_count() -> None:
    busy = _record("busy")
    free = _record("free")
    calls = []
    warnings = []
    owner = SimpleNamespace(
        _auth_switch_inflight={busy.id},
        _auth_switch_blocks_action=lambda session_id, **kwargs: warnings.append(
            (session_id, kwargs)
        )
        or True,
    )
    app = SimpleNamespace(
        windows=[owner],
        backend=SimpleNamespace(
            terminate=lambda record: calls.append(("terminate", record.id))
        ),
        store=SimpleNamespace(
            update=lambda session_id, **changes: calls.append(
                ("update", session_id, changes)
            )
        ),
        close_session_tabs=lambda session_ids: calls.append(("close", session_ids)),
    )

    terminated = TermDeckApplication.terminate_records(app, [busy, free, busy])

    assert terminated == 1
    assert warnings == [(busy.id, {"action": "stopping this tab"})]
    assert ("terminate", busy.id) not in calls
    assert ("terminate", free.id) in calls
    assert calls[-1] == ("close", {free.id})


def test_current_tab_stop_propagates_guarded_zero_result() -> None:
    busy = _record("busy")
    requested = []
    window = SimpleNamespace(
        current_session_id=lambda: busy.id,
        app_context=SimpleNamespace(
            store=SimpleNamespace(get=lambda session_id: busy if session_id == busy.id else None),
            terminate_records=lambda records: requested.append(records) or 0,
        ),
    )
    window.stop_session = lambda record: TermDeckWindow.stop_session(window, record)

    assert TermDeckWindow.stop_current_session(window) == 0
    assert requested == [[busy]]


def test_side_pane_kill_toast_uses_actual_termination_count() -> None:
    busy = _record("busy")
    free = _record("free")
    toasts = []
    app_context = SimpleNamespace(
        confirm_control_plan=lambda *_args, **_kwargs: True,
        terminate_records=lambda _records: 1,
    )
    pane = SimpleNamespace(
        window=SimpleNamespace(
            app_context=app_context,
            show_toast=lambda message, **kwargs: toasts.append((message, kwargs)),
        ),
        selected_ids={busy.id, free.id},
        refresh=lambda: None,
    )

    SidePane._kill_records(pane, [busy, free])

    assert toasts == [("Killed 1 session", {"kind": "danger"})]


def test_tab_manager_kill_uses_central_termination_guard() -> None:
    busy = _record("busy")
    calls = []
    manager = SimpleNamespace(
        app_context=SimpleNamespace(
            confirm_control_plan=lambda *_args, **_kwargs: True,
            terminate_records=lambda records: calls.append(records) or 0,
        ),
        selected_records=lambda: [busy],
        refresh=lambda: calls.append("refresh"),
    )

    MasterWindow.kill_selected(manager)

    assert calls == [[busy], "refresh"]


def test_side_pane_archive_does_not_archive_when_guarded_hide_fails() -> None:
    busy = _record("busy")
    archived = []
    pane = SimpleNamespace(
        selected_records=lambda: [busy],
        selected_ids={busy.id},
        window=SimpleNamespace(
            tabs={busy.id: object()},
            hide_tab=lambda _session_id: False,
            app_context=SimpleNamespace(store=object()),
        ),
        refresh=lambda: None,
    )

    with (
        patch("termdeck.gtk_app.Gtk.MessageDialog", _AcceptedDialog),
        patch("termdeck.gtk_app.archive_records", side_effect=lambda _store, rows: archived.extend(rows)),
    ):
        SidePane.archive_selected(pane)

    assert archived == []
    assert pane.selected_ids == {busy.id}


def test_tab_manager_archive_excludes_record_whose_guarded_hide_fails() -> None:
    busy = _record("busy")
    free = _record("free")
    archived = []
    window = SimpleNamespace(
        tabs={busy.id: object(), free.id: object()},
        hide_tab=lambda session_id: session_id != busy.id,
    )
    manager = SimpleNamespace(
        selected_records=lambda: [busy, free],
        app_context=SimpleNamespace(windows=[window], store=object()),
        refresh=lambda: None,
    )

    with (
        patch("termdeck.gtk_app.Gtk.MessageDialog", _AcceptedDialog),
        patch("termdeck.gtk_app.archive_records", side_effect=lambda _store, rows: archived.extend(rows)),
    ):
        MasterWindow.archive_selected(manager)

    assert archived == [free]
