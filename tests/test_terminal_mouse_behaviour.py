from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import AttachTerminal


class TerminalMouseBehaviourTest(TestCase):
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
