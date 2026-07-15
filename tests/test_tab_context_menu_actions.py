from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import TermDeckWindow


class MenuItemStub:
    def __init__(self, *, label: str = "") -> None:
        self.label = label
        self.handler = None
        self.submenu = None
        self.sensitive = True

    def connect(self, signal: str, handler) -> None:
        if signal != "activate":
            raise AssertionError(signal)
        self.handler = handler

    def activate(self) -> None:
        if self.handler is not None:
            self.handler(self)

    def set_submenu(self, submenu) -> None:
        self.submenu = submenu

    def set_sensitive(self, value: bool) -> None:
        self.sensitive = value


class SeparatorStub:
    label = "---"


class MenuStub:
    created = []
    raise_popup_at_pointer = False

    def __init__(self) -> None:
        self.items = []
        self.showed = False
        self.pointer_event = None
        self.fallback_args = None
        MenuStub.created.append(self)

    def append(self, item) -> None:
        self.items.append(item)

    def show_all(self) -> None:
        self.showed = True

    def popup_at_pointer(self, event) -> None:
        if self.raise_popup_at_pointer:
            raise RuntimeError("popup_at_pointer unavailable")
        self.pointer_event = event

    def popup(self, *args) -> None:
        self.fallback_args = args


def make_record(*, pinned: bool = False):
    return SimpleNamespace(id="tab-1", name="Tab 1", tag="work", pinned=pinned, tab_index=0)


def make_window(calls):
    return SimpleNamespace(
        _tab_menu_item=lambda label, handler: TermDeckWindow._tab_menu_item(SimpleNamespace(), label, handler),
        rename_session=lambda record: calls.append(("rename", record.id)),
        _pin_tab=lambda record, pinned: calls.append(("pin", record.id, pinned)),
        hide_tab=lambda session_id: calls.append(("hide", session_id)),
        _unpin_and_hide_tab=lambda record: calls.append(("unpin_hide", record.id)),
        _hide_other_tabs=lambda record: calls.append(("hide_other", record.id)),
        _hide_tabs_to_right=lambda record: calls.append(("hide_right", record.id)),
        stop_session=lambda record: calls.append(("kill", record.id)),
        _move_tab_to_new_window=lambda record: calls.append(("move_new_window", record.id)),
        set_side_pane_position=lambda position: calls.append(("side", position)),
        open_terminal_options=lambda: calls.append(("terminal_options",)),
        open_master_window=lambda: calls.append(("master",)),
        app_context=SimpleNamespace(
            available_move_windows=lambda record: [],
            move_tab_to_window=lambda session_id, target: None,
        ),
    )


class TabContextMenuActionsTest(TestCase):
    def setUp(self) -> None:
        MenuStub.created = []
        MenuStub.raise_popup_at_pointer = False

    def _show_menu(self, *, pinned: bool = False, event=None):
        calls = []
        record = make_record(pinned=pinned)
        window = make_window(calls)
        event = event or SimpleNamespace(button=3, time=42)
        with patch("termdeck.gtk_app.Gtk.Menu", MenuStub), patch(
            "termdeck.gtk_app.Gtk.MenuItem", MenuItemStub
        ), patch("termdeck.gtk_app.Gtk.SeparatorMenuItem", SeparatorStub):
            TermDeckWindow._show_tab_context_menu(window, record, event)
        # created[0] is the top-level menu; created[1] is the Move-to-window submenu.
        return MenuStub.created[0], calls, record, event

    def test_unpinned_tab_context_menu_labels_and_actions(self) -> None:
        menu, calls, _record, event = self._show_menu(pinned=False)

        self.assertEqual(
            [item.label for item in menu.items],
            [
                "Rename",
                "Pin tab",
                "---",
                "Hide tab",
                "Hide other tabs",
                "Hide tabs to the right",
                "Kill tab",
                "---",
                "Move to window",
                "---",
                "Move tabs pane to left",
                "Move tabs pane to right",
                "Terminal options",
                "Tab manager",
            ],
        )
        self.assertTrue(menu.showed)
        self.assertIs(menu.pointer_event, event)

        for item in menu.items:
            if getattr(item, "label", "---") != "---":
                item.activate()

        self.assertEqual(
            calls,
            [
                ("rename", "tab-1"),
                ("pin", "tab-1", True),
                ("hide", "tab-1"),
                ("hide_other", "tab-1"),
                ("hide_right", "tab-1"),
                ("kill", "tab-1"),
                ("side", "left"),
                ("side", "right"),
                ("terminal_options",),
                ("master",),
            ],
        )

    def test_pinned_tab_context_menu_uses_unpin_actions(self) -> None:
        menu, calls, _record, _event = self._show_menu(pinned=True)

        self.assertEqual(
            [item.label for item in menu.items],
            [
                "Rename",
                "Unpin tab",
                "---",
                "Hide tab",
                "Unpin and hide tab",
                "Hide other tabs",
                "Hide tabs to the right",
                "Kill tab",
                "---",
                "Move to window",
                "---",
                "Move tabs pane to left",
                "Move tabs pane to right",
                "Terminal options",
                "Tab manager",
            ],
        )

        for item in menu.items:
            if getattr(item, "label", "---") in {"Unpin tab", "Unpin and hide tab"}:
                item.activate()

        self.assertEqual(calls, [("pin", "tab-1", False), ("unpin_hide", "tab-1")])

    def test_tab_context_menu_falls_back_to_legacy_popup(self) -> None:
        MenuStub.raise_popup_at_pointer = True
        event = SimpleNamespace(button=2, time=99)

        menu, _calls, _record, _event = self._show_menu(pinned=False, event=event)

        self.assertEqual(menu.fallback_args, (None, None, None, None, 2, 99))


if __name__ == "__main__":
    import unittest

    unittest.main()
