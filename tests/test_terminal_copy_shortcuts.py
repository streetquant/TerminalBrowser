from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import TermDeckWindow


class KeyMapStub:
    def __init__(self, *, copy_matches: bool = False) -> None:
        self.copy_matches = copy_matches

    def matches(self, _event, action: str) -> bool:
        return self.copy_matches and action == "copy"

    def _clean_state(self, state):
        return gtk_app.KeyMap._clean_state(state)


class TerminalStub:
    def __init__(self, has_selection: bool) -> None:
        self.has_selection = has_selection

    def get_has_selection(self) -> bool:
        return self.has_selection


def make_window(*, copy_matches: bool = False, has_selection: bool = False):
    calls = []
    attach = SimpleNamespace(
        terminal=TerminalStub(has_selection),
        copy_clipboard=lambda: calls.append("copy"),
        paste_clipboard=lambda: calls.append("paste"),
    )
    window = SimpleNamespace(keymap=KeyMapStub(copy_matches=copy_matches))
    window.handle_zoom_event = lambda _event: False
    window._current_attach_terminal = lambda: attach
    window._is_copy_event = lambda event: TermDeckWindow._is_copy_event(window, event)
    window._is_plain_ctrl_c_event = lambda event: TermDeckWindow._is_plain_ctrl_c_event(window, event)
    window._is_paste_event = lambda event: TermDeckWindow._is_paste_event(window, event)
    window._terminal_has_selection = lambda terminal: TermDeckWindow._terminal_has_selection(terminal)
    return window, calls


class TerminalCopyShortcutTest(TestCase):
    def test_ctrl_shift_c_is_copy_event(self) -> None:
        window, _calls = make_window()
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_c,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK | gtk_app.Gdk.ModifierType.SHIFT_MASK,
        )

        self.assertTrue(TermDeckWindow._is_copy_event(window, event))

    def test_plain_ctrl_c_is_not_unconditional_copy_event(self) -> None:
        window, _calls = make_window()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_c, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        self.assertFalse(TermDeckWindow._is_copy_event(window, event))
        self.assertTrue(TermDeckWindow._is_plain_ctrl_c_event(window, event))

    def test_ctrl_c_with_selection_copies_and_blocks_shell_interrupt(self) -> None:
        window, calls = make_window(has_selection=True)
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_c, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["copy"])

    def test_ctrl_c_without_selection_passes_through_to_shell(self) -> None:
        window, calls = make_window(has_selection=False)
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_c, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_keymap_copy_binding_still_copies_without_selection(self) -> None:
        window, calls = make_window(copy_matches=True, has_selection=False)
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_c, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["copy"])


if __name__ == "__main__":
    import unittest

    unittest.main()
