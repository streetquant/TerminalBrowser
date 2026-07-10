from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import TermDeckWindow


class KeyMapStub:
    def __init__(self, *, paste_matches: bool = False) -> None:
        self.paste_matches = paste_matches

    def matches(self, _event, action: str) -> bool:
        return self.paste_matches and action == "paste"

    def _clean_state(self, state):
        return gtk_app.KeyMap._clean_state(state)


def make_window(*, paste_matches: bool = False, attach=None):
    window = SimpleNamespace(keymap=KeyMapStub(paste_matches=paste_matches))
    window._is_copy_event = lambda event: TermDeckWindow._is_copy_event(window, event)
    window._is_plain_ctrl_c_event = lambda event: TermDeckWindow._is_plain_ctrl_c_event(window, event)
    window._is_paste_event = lambda event: TermDeckWindow._is_paste_event(window, event)
    window._terminal_has_selection = lambda terminal: TermDeckWindow._terminal_has_selection(terminal)
    if attach is not None:
        window._current_attach_terminal = lambda: attach
    return window


class TerminalPasteShortcutTest(TestCase):
    def test_ctrl_v_is_paste_event(self) -> None:
        window = make_window()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_v, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        self.assertTrue(TermDeckWindow._is_paste_event(window, event))

    def test_shift_insert_is_paste_event(self) -> None:
        window = make_window()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_Insert, state=gtk_app.Gdk.ModifierType.SHIFT_MASK)

        self.assertTrue(TermDeckWindow._is_paste_event(window, event))

    def test_ctrl_shift_v_is_unconditional_system_clipboard_paste(self) -> None:
        window = make_window(paste_matches=False)
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_v,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK | gtk_app.Gdk.ModifierType.SHIFT_MASK,
        )

        self.assertTrue(TermDeckWindow._is_paste_event(window, event))

    def test_keymap_paste_binding_is_respected(self) -> None:
        window = make_window(paste_matches=True)
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_F8, state=0)

        self.assertTrue(TermDeckWindow._is_paste_event(window, event))

    def test_plain_v_is_not_paste_event(self) -> None:
        window = make_window()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_v, state=0)

        self.assertFalse(TermDeckWindow._is_paste_event(window, event))

    def test_terminal_key_handler_routes_paste_to_current_terminal(self) -> None:
        calls = []
        attach = SimpleNamespace(paste_clipboard=lambda: calls.append("paste"), copy_clipboard=lambda: calls.append("copy"))
        window = make_window(attach=attach)
        window.handle_zoom_event = lambda _event: False
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_v, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["paste"])

    def test_ctrl_shift_v_handler_routes_system_clipboard_paste_without_keymap_match(self) -> None:
        calls = []
        attach = SimpleNamespace(paste_clipboard=lambda: calls.append("clipboard"), copy_clipboard=lambda: calls.append("copy"))
        window = make_window(paste_matches=False, attach=attach)
        window.handle_zoom_event = lambda _event: False
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_v,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK | gtk_app.Gdk.ModifierType.SHIFT_MASK,
        )

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["clipboard"])


if __name__ == "__main__":
    import unittest

    unittest.main()
