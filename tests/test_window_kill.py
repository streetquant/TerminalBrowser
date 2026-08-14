from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from termdeck.gtk_app import Gtk, TermDeckApplication, TermDeckWindow
from termdeck.session_store import SessionStore


class _KillDialog:
    response = Gtk.ResponseType.OK
    instances: list["_KillDialog"] = []

    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs
        self.secondary = ""
        self.destroyed = False
        self.__class__.instances.append(self)

    def format_secondary_text(self, text: str) -> None:
        self.secondary = text

    def run(self):
        return self.response

    def destroy(self) -> None:
        self.destroyed = True


def _bind_application_methods(app: SimpleNamespace) -> None:
    app.confirm_kill_window = lambda parent, records: TermDeckApplication.confirm_kill_window(
        app,
        parent,
        records,
    )
    app.terminate_records = lambda records: TermDeckApplication.terminate_records(app, records)


def test_window_menu_exposes_explicit_permanent_kill_action() -> None:
    source = inspect.getsource(TermDeckWindow.__init__)

    assert '("Kill Window Permanently…", self.kill_window)' in source


def test_cancelled_window_kill_preserves_sessions_and_restore_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.sqlite3")
        try:
            record = store.create(
                "worker",
                "dev",
                "bash",
                tmp,
                window_id="win-cancel",
                window_title="Keep Me",
            )
            terminated: list[str] = []
            destroyed: list[bool] = []
            window = SimpleNamespace(
                window_id="win-cancel",
                window_title="Keep Me",
                _auth_switch_inflight=set(),
                show_toast=lambda *_args, **_kwargs: None,
                destroy=lambda: destroyed.append(True),
            )
            app = SimpleNamespace(
                store=store,
                windows=[window],
                backend=SimpleNamespace(
                    terminate=lambda candidate: terminated.append(candidate.id)
                ),
                close_session_tabs=lambda _session_ids: None,
            )
            _bind_application_methods(app)
            _KillDialog.instances.clear()
            _KillDialog.response = Gtk.ResponseType.CANCEL

            with patch("termdeck.gtk_app.Gtk.MessageDialog", _KillDialog):
                removed = TermDeckApplication.kill_window(app, window)

            assert removed == 0
            assert terminated == []
            assert destroyed == []
            assert store.get(record.id) is not None
            assert store.remembered_windows() == [("win-cancel", "Keep Me")]
            dialog = _KillDialog.instances[-1]
            assert dialog.kwargs["text"] == "Kill “Keep Me” permanently?"
            assert "cannot return after restart" in dialog.secondary
            assert "Closing the window instead keeps it" in dialog.secondary
            assert dialog.destroyed
        finally:
            store.close()


def test_window_kill_stops_managed_session_but_only_forgets_external_reference() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sessions.sqlite3"
        store = SessionStore(db)
        terminated: list[str] = []
        closed: list[set[str]] = []
        destroyed: list[bool] = []
        toasts: list[tuple[str, str]] = []
        managed = store.create(
            "managed",
            "dev",
            "bash",
            tmp,
            window_id="win-kill",
            window_title="Disposable",
        )
        managed = store.update(managed.id, status="running", last_pid=101)
        external = store.create(
            "external",
            "dev",
            "bash",
            tmp,
            window_id="win-kill",
            window_title="Disposable",
            tab_index=1,
        )
        external = store.update(
            external.id,
            status="running",
            last_pid=202,
            external=True,
        )
        window = SimpleNamespace(
            window_id="win-kill",
            window_title="Disposable",
            _auth_switch_inflight=set(),
            show_toast=lambda message, kind: toasts.append((message, kind)),
            destroy=lambda: destroyed.append(True),
        )
        app = SimpleNamespace(
            store=store,
            windows=[window],
            backend=SimpleNamespace(
                terminate=lambda candidate: terminated.append(candidate.id)
            ),
            close_session_tabs=lambda session_ids: closed.append(set(session_ids)),
            confirm_kill_window=lambda _parent, _records: True,
        )
        app.terminate_records = lambda records: TermDeckApplication.terminate_records(
            app,
            records,
        )

        try:
            removed = TermDeckApplication.kill_window(app, window)

            assert removed == 2
            assert terminated == [managed.id]
            assert {managed.id} in closed
            assert {external.id} in closed
            assert store.get(managed.id) is None
            assert store.get(external.id) is None
            assert store.remembered_windows() == []
            assert destroyed == [True]
            assert len(toasts) == 1
            assert "stopped 1 managed session" in toasts[0][0]
            assert "forgot 1 external reference without stopping tmux" in toasts[0][0]
            assert "removed 2 saved tabs" in toasts[0][0]
            assert toasts[0][1] == "danger"
        finally:
            store.close()

        reopened = SessionStore(db)
        try:
            assert reopened.windows_for_restore() == []
        finally:
            reopened.close()


def test_window_kill_preserves_auth_guarded_row_and_window_for_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.sqlite3")
        try:
            busy = store.create(
                "busy",
                "dev",
                "bash",
                tmp,
                window_id="win-partial",
                window_title="Partial",
            )
            busy = store.update(busy.id, status="running", last_pid=301)
            free = store.create(
                "free",
                "dev",
                "bash",
                tmp,
                window_id="win-partial",
                window_title="Partial",
                tab_index=1,
            )
            free = store.update(free.id, status="running", last_pid=302)
            external = store.create(
                "external",
                "dev",
                "bash",
                tmp,
                window_id="win-partial",
                window_title="Partial",
                tab_index=2,
            )
            external = store.update(external.id, status="running", external=True)
            terminated: list[str] = []
            closed: list[set[str]] = []
            warnings: list[tuple[str, str]] = []
            toasts: list[tuple[str, str]] = []
            destroyed: list[bool] = []
            window = SimpleNamespace(
                window_id="win-partial",
                window_title="Partial",
                _auth_switch_inflight={busy.id},
                _auth_switch_blocks_action=lambda session_id, *, action: warnings.append(
                    (session_id, action)
                )
                or True,
                show_toast=lambda message, kind: toasts.append((message, kind)),
                destroy=lambda: destroyed.append(True),
            )
            app = SimpleNamespace(
                store=store,
                windows=[window],
                backend=SimpleNamespace(
                    terminate=lambda candidate: terminated.append(candidate.id)
                ),
                close_session_tabs=lambda session_ids: closed.append(set(session_ids)),
                confirm_kill_window=lambda _parent, _records: True,
            )
            app.terminate_records = lambda records: TermDeckApplication.terminate_records(
                app,
                records,
            )

            removed = TermDeckApplication.kill_window(app, window)

            assert removed == 2
            assert terminated == [free.id]
            assert warnings == [(busy.id, "stopping this tab")]
            assert store.get(busy.id) is not None
            assert store.get(free.id) is None
            assert store.get(external.id) is None
            assert store.remembered_windows() == [("win-partial", "Partial")]
            assert [record.id for record in store.windows_for_restore()[0][2]] == [busy.id]
            assert destroyed == []
            assert {free.id} in closed
            assert {external.id} in closed
            assert len(toasts) == 1
            assert "stopped 1 managed session" in toasts[0][0]
            assert "removed 2 of 3 tabs" in toasts[0][0]
            assert "1 tab remains" in toasts[0][0]
            assert "1 Codex account switch is still in progress" in toasts[0][0]
            assert toasts[0][1] == "warning"
        finally:
            store.close()


def test_window_kill_preserves_rows_when_central_termination_fails() -> None:
    record = SimpleNamespace(
        id="failed",
        name="Failed",
        window_id="win-failed",
        external=False,
        status="running",
        visible=True,
        last_pid=404,
        recovery_command="codex resume thread-id",
        recovery_cwd="/tmp",
    )
    rows = {record.id: record}
    deleted: list[str] = []
    forgotten: list[str] = []
    destroyed: list[bool] = []
    toasts: list[tuple[str, str]] = []

    class _Store:
        @staticmethod
        def list(**_kwargs):
            return list(rows.values())

        @staticmethod
        def get(session_id):
            return rows.get(session_id)

        @staticmethod
        def delete(session_id):
            deleted.append(session_id)
            rows.pop(session_id, None)

        @staticmethod
        def forget_window(window_id):
            forgotten.append(window_id)
            return True

    window = SimpleNamespace(
        window_id="win-failed",
        window_title="Failed",
        _auth_switch_inflight=set(),
        show_toast=lambda message, kind: toasts.append((message, kind)),
        destroy=lambda: destroyed.append(True),
    )
    app = SimpleNamespace(
        store=_Store(),
        windows=[window],
        confirm_kill_window=lambda _parent, _records: True,
        terminate_records=lambda _records: (_ for _ in ()).throw(
            RuntimeError("backend unavailable")
        ),
        close_session_tabs=lambda _session_ids: None,
    )

    removed = TermDeckApplication.kill_window(app, window)

    assert removed == 0
    assert rows == {record.id: record}
    assert deleted == []
    assert forgotten == []
    assert destroyed == []
    assert "removed 0 of 1 tab" in toasts[0][0]
    assert "1 tab remains" in toasts[0][0]
    assert toasts[0][1] == "danger"


def test_forget_window_refuses_records_and_removes_empty_restore_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.sqlite3")
        try:
            record = store.create(
                "worker",
                "dev",
                "bash",
                tmp,
                window_id="win-store",
                window_title="Store",
            )

            assert not store.forget_window("win-store")
            assert store.remembered_windows() == [("win-store", "Store")]

            store.delete(record.id)

            assert store.forget_window("win-store")
            assert store.remembered_windows() == []
            assert store.windows_for_restore() == []
        finally:
            store.close()
