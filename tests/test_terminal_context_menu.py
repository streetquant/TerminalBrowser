from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck import gtk_app
from termdeck.gtk_app import AttachTerminal


class FakeMenuItem:
    def __init__(self, *, label: str) -> None:
        self.label = label
        self.handler = None

    def connect(self, signal: str, handler) -> None:
        if signal != "activate":
            raise AssertionError(f"unexpected signal: {signal}")
        self.handler = handler

    def activate(self) -> None:
        if self.handler is None:
            raise AssertionError("menu item has no activate handler")
        self.handler(self)


class FakeMenu:
    latest = None
    raise_at_pointer = False

    def __init__(self) -> None:
        self.items = []
        self.shown = False
        self.popup_at_pointer_event = None
        self.popup_fallback_args = None
        FakeMenu.latest = self

    def append(self, item: FakeMenuItem) -> None:
        self.items.append(item)

    def show_all(self) -> None:
        self.shown = True

    def popup_at_pointer(self, event) -> None:
        if self.raise_at_pointer:
            raise RuntimeError("popup_at_pointer unavailable")
        self.popup_at_pointer_event = event

    def popup(self, *args) -> None:
        self.popup_fallback_args = args


class TerminalContextMenuTest(TestCase):
    def tearDown(self) -> None:
        FakeMenu.latest = None
        FakeMenu.raise_at_pointer = False

    def test_context_menu_exposes_terminal_clipboard_actions(self) -> None:
        calls = []
        terminal = SimpleNamespace(
            select_all=lambda: calls.append("select_all"),
            unselect_all=lambda: calls.append("unselect_all"),
        )
        attach = SimpleNamespace(
            terminal=terminal,
            copy_clipboard=lambda: calls.append("copy_clipboard"),
            paste_clipboard=lambda: calls.append("paste_clipboard"),
            paste_primary=lambda: calls.append("paste_primary"),
        )
        event = SimpleNamespace(button=3, time=123)

        with patch.object(gtk_app.Gtk, "Menu", FakeMenu), patch.object(gtk_app.Gtk, "MenuItem", FakeMenuItem):
            AttachTerminal._show_terminal_context_menu(attach, event)

        menu = FakeMenu.latest
        self.assertIsNotNone(menu)
        self.assertTrue(menu.shown)
        self.assertIs(menu.popup_at_pointer_event, event)
        self.assertEqual(
            [item.label for item in menu.items],
            ["Copy selection", "Paste", "Paste primary", "Select all", "Clear selection"],
        )

        for item in menu.items:
            item.activate()

        self.assertEqual(
            calls,
            ["copy_clipboard", "paste_clipboard", "paste_primary", "select_all", "unselect_all"],
        )

    def test_context_menu_falls_back_when_popup_at_pointer_is_unavailable(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(select_all=lambda: None, unselect_all=lambda: None),
            copy_clipboard=lambda: calls.append("copy"),
            paste_clipboard=lambda: calls.append("paste"),
            paste_primary=lambda: calls.append("primary"),
        )
        event = SimpleNamespace(button=3, time=456)
        FakeMenu.raise_at_pointer = True

        with patch.object(gtk_app.Gtk, "Menu", FakeMenu), patch.object(gtk_app.Gtk, "MenuItem", FakeMenuItem):
            AttachTerminal._show_terminal_context_menu(attach, event)

        menu = FakeMenu.latest
        self.assertIsNotNone(menu)
        self.assertIsNone(menu.popup_at_pointer_event)
        self.assertIsNotNone(menu.popup_fallback_args)
        self.assertEqual(menu.popup_fallback_args[-2:], (3, 456))


if __name__ == "__main__":
    import unittest

    unittest.main()
