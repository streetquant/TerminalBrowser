from types import SimpleNamespace
from unittest import TestCase
from unittest import mock

from termdeck.gtk_app import AttachTerminal, Gtk
from termdeck.models import TerminalTheme


class TerminalMouseBehaviourTest(TestCase):
    def test_terminal_has_real_scroller_and_fallback_scroll_support(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=1), mock.patch(
            "termdeck.gtk_app.GLib.source_remove"
        ):
            attach = AttachTerminal(["true"], "/tmp", TerminalTheme())
            try:
                self.assertIs(attach.scroller.get_child(), attach.terminal)
                horizontal, vertical = attach.scroller.get_policy()
                self.assertEqual(horizontal, Gtk.PolicyType.NEVER)
                self.assertEqual(vertical, Gtk.PolicyType.AUTOMATIC)
                self.assertTrue(attach.terminal.get_enable_fallback_scrolling())
                self.assertFalse(attach.terminal.get_scroll_on_output())
                self.assertTrue(attach.terminal.get_scroll_on_keystroke())
            finally:
                attach.destroy()

    def test_middle_click_pastes_primary_and_consumes_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=2)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["paste_primary"])

    def test_right_click_opens_terminal_context_menu_and_consumes_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=3)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [("menu", event)])

    def test_left_click_does_not_consume_press_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=1)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_missing_button_does_not_consume_press_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace()

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
