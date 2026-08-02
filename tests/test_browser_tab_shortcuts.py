from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import TermDeckWindow


class KeyMapStub:
    def __init__(self, *matching_actions: str) -> None:
        self.matching_actions = set(matching_actions)

    def matches(self, _event, action: str) -> bool:
        return action in self.matching_actions

    def _clean_state(self, state):
        return gtk_app.KeyMap._clean_state(state)


def make_terminal_window(*matching_actions: str):
    calls: list[object] = []
    window = SimpleNamespace(keymap=KeyMapStub(*matching_actions))
    window.new_tab = lambda: calls.append("new")
    window.quit_termdeck = lambda: calls.append("quit")
    window.close_current_tab = lambda: calls.append("close")
    window.reopen_last_closed_tab = lambda: calls.append("reopen")
    window.jump_to_tab = lambda index: calls.append(("jump", index))
    window.handle_zoom_event = lambda _event: False
    window._current_attach_terminal = lambda: None
    return window, calls


def make_window_handler():
    calls: list[object] = []
    window = SimpleNamespace(keymap=KeyMapStub())
    window.handle_zoom_event = lambda _event: False
    window.jump_to_tab = lambda index: calls.append(("jump", index))
    window.toggle_pause_tab = lambda index: calls.append(("pause", index))
    window._route_unhandled_key_to_terminal = lambda _event: False
    return window, calls


class BrowserTabShortcutTest(TestCase):
    def test_ctrl_shift_t_reopens_while_terminal_has_focus(self) -> None:
        window, calls = make_terminal_window("reopen_closed")
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_t,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK | gtk_app.Gdk.ModifierType.SHIFT_MASK,
        )

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["reopen"])

    def test_ctrl_digit_jumps_while_terminal_has_focus(self) -> None:
        window, calls = make_terminal_window()
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_2,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK,
        )

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [("jump", 2)])

    def test_ctrl_digit_jumps_from_window_chrome(self) -> None:
        window, calls = make_window_handler()
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_3,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK,
        )

        handled = TermDeckWindow.on_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [("jump", 3)])

    def test_ctrl_alt_digit_keeps_pause_shortcut(self) -> None:
        window, calls = make_window_handler()
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_4,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK | gtk_app.Gdk.ModifierType.MOD1_MASK,
        )

        handled = TermDeckWindow.on_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [("pause", 4)])


if __name__ == "__main__":
    import unittest

    unittest.main()
