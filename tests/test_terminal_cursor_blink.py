from unittest import TestCase

from termdeck.gtk_app import Vte, apply_theme
from termdeck.models import TerminalTheme


class TerminalStub:
    def __init__(self, *, fail_blink: bool = False) -> None:
        self.fail_blink = fail_blink
        self.calls = []

    def set_font(self, font) -> None:
        self.calls.append(("font", font))

    def set_scrollback_lines(self, lines: int) -> None:
        self.calls.append(("scrollback", lines))

    def set_colors(self, foreground, background, palette) -> None:
        self.calls.append(("colors", foreground, background, palette))

    def set_color_cursor(self, colour) -> None:
        self.calls.append(("cursor_color", colour))

    def set_cursor_shape(self, shape) -> None:
        self.calls.append(("cursor_shape", shape))

    def feed(self, data) -> None:
        self.calls.append(("feed", bytes(data)))

    def set_cursor_blink_mode(self, mode) -> None:
        if self.fail_blink:
            raise RuntimeError("blink mode unsupported")
        self.calls.append(("cursor_blink", mode))


class TerminalCursorBlinkTest(TestCase):
    def test_apply_theme_turns_cursor_blink_on(self) -> None:
        terminal = TerminalStub()

        apply_theme(terminal, TerminalTheme(cursor_blink=True))

        self.assertIn(("cursor_blink", Vte.CursorBlinkMode.ON), terminal.calls)

    def test_apply_theme_turns_cursor_blink_off(self) -> None:
        terminal = TerminalStub()

        apply_theme(terminal, TerminalTheme(cursor_blink=False))

        self.assertIn(("cursor_blink", Vte.CursorBlinkMode.OFF), terminal.calls)

    def test_apply_theme_leaves_cursor_blink_unchanged_when_unset(self) -> None:
        terminal = TerminalStub()

        apply_theme(terminal, TerminalTheme(cursor_blink=None))

        self.assertNotIn("cursor_blink", [name for name, *_rest in terminal.calls])

    def test_apply_theme_ignores_vte_without_cursor_blink_support(self) -> None:
        terminal = TerminalStub(fail_blink=True)

        apply_theme(terminal, TerminalTheme(cursor_blink=False))

        self.assertNotIn("cursor_blink", [name for name, *_rest in terminal.calls])


if __name__ == "__main__":
    import unittest

    unittest.main()
