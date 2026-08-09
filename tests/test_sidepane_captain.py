import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import SidePane
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxError


class EntryStub:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text


class BackendStub:
    def __init__(self, codex_ids=(), *, send_error: Exception | None = None) -> None:
        self.codex_ids = set(codex_ids)
        self.send_error = send_error
        self.sent = []

    def is_codex_running(self, record) -> bool:
        return record.id in self.codex_ids

    def send_text(self, record, text: str, enter: bool = True) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((record, text, enter))


def bind_side(window, text: str):
    pane = SimpleNamespace(window=window, prompt=EntryStub(text), selected_ids=set())
    for name in (
        "_stored_captain",
        "_captain_error",
        "_set_captain",
        "resolve_captain",
        "make_captain",
        "selected_records",
    ):
        setattr(pane, name, MethodType(getattr(SidePane, name), pane))
    return pane


class SidePaneCaptainTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SessionStore(Path(self.tempdir.name) / "sessions.sqlite3")
        self.addCleanup(self.store.close)

    def record(self, name: str, *, window_id: str = "win-a", index: int = 0):
        record = self.store.create(
            name,
            "terminal",
            "/bin/bash",
            self.tempdir.name,
            window_id=window_id,
            window_title="Window A" if window_id == "win-a" else "Window B",
            tab_index=index,
        )
        return self.store.update(record.id, status="running", last_pid=100 + index)

    def window(self, records, current, backend):
        toasts = []
        confirmations = []
        refreshes = []
        app_context = SimpleNamespace(
            store=self.store,
            backend=backend,
            confirm_control_plan=lambda _parent, targets, action, **kwargs: (
                confirmations.append((targets, action, kwargs)) or True
            ),
        )
        window = SimpleNamespace(
            app_context=app_context,
            window_id="win-a",
            window_title="Window A",
            tabs={record.id: object() for record in records if record.window_id == "win-a"},
            current_record=lambda: current,
            refresh_captain_chrome=lambda: refreshes.append(True),
            show_toast=lambda message, kind="info": toasts.append((message, kind)),
        )
        return window, toasts, confirmations, refreshes

    def test_send_without_selection_adopts_current_codex_and_never_broadcasts(self) -> None:
        captain = self.record("captain", index=0)
        worker = self.record("worker", index=1)
        foreign = self.record("foreign captain", window_id="win-b", index=0)
        backend = BackendStub({captain.id, foreign.id})
        window, toasts, confirmations, refreshes = self.window(
            [captain, worker, foreign], captain, backend
        )
        pane = bind_side(window, "coordinate this exact task")
        pane.selected_ids.update({worker.id, foreign.id})

        SidePane.send_prompt(pane)

        self.assertEqual(len(backend.sent), 1)
        target, envelope, enter = backend.sent[0]
        self.assertEqual(target.id, captain.id)
        self.assertTrue(enter)
        self.assertIn("coordinate this exact task", envelope)
        self.assertIn(worker.id, envelope)
        self.assertNotIn(foreign.id, envelope)
        self.assertEqual(pane.prompt.text, "")
        self.assertEqual(self.store.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
        self.assertEqual(confirmations[0][0][0].id, captain.id)
        self.assertEqual(len(confirmations[0][0]), 1)
        self.assertEqual(confirmations[0][2]["selection"], "window Codex captain")
        self.assertTrue(refreshes)
        self.assertIn(("Sent to captain captain", "success"), toasts)

    def test_persisted_captain_is_used_when_another_tab_is_current(self) -> None:
        captain = self.record("captain", index=0)
        worker = self.record("worker", index=1)
        self.store.set_window_captain("win-a", captain.id)
        backend = BackendStub({captain.id})
        window, _toasts, _confirmations, _refreshes = self.window(
            [captain, worker], worker, backend
        )
        pane = bind_side(window, "keep coordinating")

        SidePane.send_prompt(pane)

        self.assertEqual([call[0].id for call in backend.sent], [captain.id])

    def test_non_codex_current_tab_fails_loudly_and_retains_prompt(self) -> None:
        worker = self.record("worker")
        backend = BackendStub()
        window, toasts, confirmations, _refreshes = self.window([worker], worker, backend)
        pane = bind_side(window, "do not lose this")

        SidePane.send_prompt(pane)

        self.assertEqual(backend.sent, [])
        self.assertEqual(confirmations, [])
        self.assertEqual(pane.prompt.text, "do not lose this")
        self.assertTrue(toasts)
        self.assertIn("not currently running Codex", toasts[-1][0])
        self.assertEqual(toasts[-1][1], "warn")

    def test_send_failure_retains_prompt_and_reports_error(self) -> None:
        captain = self.record("captain")
        self.store.set_window_captain("win-a", captain.id)
        backend = BackendStub({captain.id}, send_error=TmuxError("missing pane"))
        window, toasts, confirmations, _refreshes = self.window([captain], captain, backend)
        pane = bind_side(window, "retry me")

        SidePane.send_prompt(pane)

        self.assertEqual(len(confirmations), 1)
        self.assertEqual(pane.prompt.text, "retry me")
        self.assertEqual(toasts[-1], ("Could not send to captain: missing pane", "danger"))

    def test_selected_records_revalidates_window_membership(self) -> None:
        local = self.record("local")
        foreign = self.record("foreign", window_id="win-b")
        backend = BackendStub()
        window, *_rest = self.window([local, foreign], local, backend)
        pane = bind_side(window, "unused")
        pane.selected_ids.update({local.id, foreign.id})

        records = SidePane.selected_records(pane)

        self.assertEqual([record.id for record in records], [local.id])


if __name__ == "__main__":
    import unittest

    unittest.main()
