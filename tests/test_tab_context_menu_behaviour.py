from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import TermDeckWindow
from termdeck.models import SessionRecord


def make_record() -> SessionRecord:
    return SessionRecord(
        id="session-1",
        name="dev",
        tag="work",
        command="bash",
        cwd="/tmp",
        tmux_name="termdeck-session-1",
        created_at="2026-07-09T00:00:00Z",
        last_opened_at="2026-07-09T00:00:00Z",
        status="running",
        last_pid=1234,
    )


class StoreStub:
    def __init__(self, record: SessionRecord | None) -> None:
        self.record = record

    def get(self, session_id: str) -> SessionRecord | None:
        if self.record is not None and self.record.id == session_id:
            return self.record
        return None


class TabContextMenuBehaviourTest(TestCase):
    def test_right_click_opens_tab_context_menu_for_existing_record(self) -> None:
        record = make_record()
        opened = []
        renamed = []
        window = SimpleNamespace(
            app_context=SimpleNamespace(store=StoreStub(record)),
            _show_tab_context_menu=lambda rec, event: opened.append((rec, event)),
            rename_session=lambda rec: renamed.append(rec),
        )
        event = SimpleNamespace(button=3, type=None)

        handled = TermDeckWindow._on_tab_label_press(window, None, event, record.id)

        self.assertTrue(handled)
        self.assertEqual(opened, [(record, event)])
        self.assertEqual(renamed, [])

    def test_double_click_renames_existing_record_without_opening_menu(self) -> None:
        record = make_record()
        opened = []
        renamed = []
        window = SimpleNamespace(
            app_context=SimpleNamespace(store=StoreStub(record)),
            _show_tab_context_menu=lambda rec, event: opened.append((rec, event)),
            rename_session=lambda rec: renamed.append(rec),
        )
        event = SimpleNamespace(button=1, type=gtk_app.Gdk.EventType.DOUBLE_BUTTON_PRESS)

        handled = TermDeckWindow._on_tab_label_press(window, None, event, record.id)

        self.assertTrue(handled)
        self.assertEqual(renamed, [record])
        self.assertEqual(opened, [])

    def test_right_click_on_missing_record_is_not_handled(self) -> None:
        opened = []
        window = SimpleNamespace(
            app_context=SimpleNamespace(store=StoreStub(None)),
            _show_tab_context_menu=lambda rec, event: opened.append((rec, event)),
            rename_session=lambda rec: None,
        )
        event = SimpleNamespace(button=3, type=None)

        handled = TermDeckWindow._on_tab_label_press(window, None, event, "missing")

        self.assertFalse(handled)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
