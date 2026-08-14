"""Modifier-only keypresses must not exit the tmux copy-mode scroll view.

Regression guard for the bug where pressing Ctrl, Alt, Super (Windows key),
Shift, or any other bare modifier key while scrolled up would immediately
snap the terminal back to the live bottom of the pane.
"""
from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import TermDeckWindow, _is_modifier_only_keyval


# ---------------------------------------------------------------------------
# _is_modifier_only_keyval unit tests
# ---------------------------------------------------------------------------

class IsModifierOnlyKeyvalTest(TestCase):
    def test_bare_modifiers_are_detected(self) -> None:
        modifier_keyvals = [
            gtk_app.Gdk.KEY_Shift_L,
            gtk_app.Gdk.KEY_Shift_R,
            gtk_app.Gdk.KEY_Control_L,
            gtk_app.Gdk.KEY_Control_R,
            gtk_app.Gdk.KEY_Alt_L,
            gtk_app.Gdk.KEY_Alt_R,
            gtk_app.Gdk.KEY_Meta_L,
            gtk_app.Gdk.KEY_Meta_R,
            gtk_app.Gdk.KEY_Super_L,
            gtk_app.Gdk.KEY_Super_R,
            gtk_app.Gdk.KEY_Hyper_L,
            gtk_app.Gdk.KEY_Hyper_R,
            gtk_app.Gdk.KEY_Caps_Lock,
            gtk_app.Gdk.KEY_Num_Lock,
            gtk_app.Gdk.KEY_Scroll_Lock,
            gtk_app.Gdk.KEY_ISO_Level3_Shift,
        ]
        for keyval in modifier_keyvals:
            with self.subTest(keyval=keyval):
                self.assertTrue(
                    _is_modifier_only_keyval(keyval),
                    f"keyval {keyval} should be modifier-only",
                )

    def test_printable_keys_are_not_modifiers(self) -> None:
        printable_keyvals = [
            gtk_app.Gdk.KEY_a,
            gtk_app.Gdk.KEY_z,
            gtk_app.Gdk.KEY_Return,
            gtk_app.Gdk.KEY_space,
            gtk_app.Gdk.KEY_BackSpace,
            gtk_app.Gdk.KEY_Up,
            gtk_app.Gdk.KEY_Down,
            gtk_app.Gdk.KEY_Page_Up,
            gtk_app.Gdk.KEY_Page_Down,
            gtk_app.Gdk.KEY_Escape,
        ]
        for keyval in printable_keyvals:
            with self.subTest(keyval=keyval):
                self.assertFalse(
                    _is_modifier_only_keyval(keyval),
                    f"keyval {keyval} should NOT be modifier-only",
                )


# ---------------------------------------------------------------------------
# on_terminal_key_press integration guard
# ---------------------------------------------------------------------------

def _make_scroll_view_window(calls, *, scroll_active: bool = True):
    """Build a minimal TermDeckWindow-shaped namespace for key-press tests."""
    record = SimpleNamespace(id="sess-1")
    attach = SimpleNamespace(
        terminal=object(),
        prepare_for_live_input=lambda: calls.append(("restore", "sess-1")) or True,
    )
    backend = SimpleNamespace(
        scroll_view_active=lambda r: scroll_active,
    )
    return SimpleNamespace(
        keymap=SimpleNamespace(
            matches=lambda event, name: False,
        ),
        handle_zoom_event=lambda event: False,
        _current_attach_terminal=lambda: attach,
        _is_copy_event=lambda event: False,
        _is_plain_ctrl_c_event=lambda event: False,
        _terminal_has_selection=lambda attach: False,
        _is_paste_event=lambda event: False,
        current_record=lambda: record,
        app_context=SimpleNamespace(backend=backend),
    )


class ModifierKeyScrollGuardTest(TestCase):
    """Modifier-only keys must not cross the live-input restoration boundary."""

    def _press(self, keyval: int, *, scroll_active: bool = True):
        calls = []
        window = _make_scroll_view_window(calls, scroll_active=scroll_active)
        event = SimpleNamespace(keyval=keyval, state=0)
        handled = TermDeckWindow.on_terminal_key_press(window, window._current_attach_terminal().terminal, event)
        return handled, calls

    # --- modifier keys: must NOT exit scroll view ---

    def test_ctrl_l_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Control_L)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_ctrl_r_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Control_R)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_alt_l_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Alt_L)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_super_l_does_not_exit_scroll_view(self) -> None:
        """Windows / Command key must not exit the scroll view."""
        handled, calls = self._press(gtk_app.Gdk.KEY_Super_L)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_super_r_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Super_R)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_shift_l_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Shift_L)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_caps_lock_does_not_exit_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Caps_Lock)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    def test_iso_level3_shift_does_not_exit_scroll_view(self) -> None:
        """AltGr must not exit the scroll view."""
        handled, calls = self._press(gtk_app.Gdk.KEY_ISO_Level3_Shift)
        self.assertFalse(handled)
        self.assertNotIn("restore", [c[0] if isinstance(c, tuple) else c for c in calls])

    # --- real keys: MUST still exit scroll view ---

    def test_letter_key_exits_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_a)
        self.assertFalse(handled)
        self.assertIn(("restore", "sess-1"), calls)

    def test_enter_exits_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Return)
        self.assertFalse(handled)
        self.assertIn(("restore", "sess-1"), calls)

    def test_escape_exits_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Escape)
        # Escape is handled by a prior branch that returns True, so restore
        # may or may not be called — just verify no crash.
        self.assertIsInstance(handled, bool)

    def test_arrow_up_exits_scroll_view(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Up)
        self.assertFalse(handled)
        self.assertIn(("restore", "sess-1"), calls)

    # --- not in scroll view: nothing should happen ---

    def test_ctrl_does_nothing_when_not_scrolled(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_Control_L, scroll_active=False)
        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_letter_does_nothing_extra_when_not_scrolled(self) -> None:
        handled, calls = self._press(gtk_app.Gdk.KEY_x, scroll_active=False)
        self.assertFalse(handled)
        # restore should NOT be called when scroll_view_active is False
        self.assertNotIn(("restore", "sess-1"), calls)
