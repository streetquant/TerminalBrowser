"""Terminal legibility guarantees: minimum-contrast floors for every theme.

Regression suite for the black-on-black rendering bug: palette packs (and
imported Terminator themes) could carry an ANSI color0 equal or nearly equal
to the theme background, making SGR-30 text invisible. The contrast guard in
``ensure_legible_theme`` must make that impossible at the VTE choke point.
"""

from __future__ import annotations

import inspect
import unittest

from termdeck.models import DEFAULT_PALETTE, TerminalTheme
from termdeck.settings import AppSettings
from termdeck.ui_theme import (
    TERMINAL_MIN_CURSOR_CONTRAST,
    TERMINAL_MIN_FG_CONTRAST,
    TERMINAL_MIN_PALETTE_CONTRAST,
    apply_terminal_palette,
    contrast_ratio,
    ensure_legible_theme,
    normalise_terminal_palette,
    raise_contrast,
    terminal_palette_names,
)


class EveryPackClearsTheFloors(unittest.TestCase):
    def test_all_packs_meet_minimum_contrast_after_guard(self):
        base = TerminalTheme()
        for name in terminal_palette_names():
            themed = ensure_legible_theme(apply_terminal_palette(base, name))
            background = themed.background_color
            self.assertGreaterEqual(
                contrast_ratio(themed.foreground_color, background),
                TERMINAL_MIN_FG_CONTRAST,
                name,
            )
            self.assertEqual(len(themed.palette), 16, name)
            for index, entry in enumerate(themed.palette):
                self.assertGreaterEqual(
                    contrast_ratio(entry, background),
                    TERMINAL_MIN_PALETTE_CONTRAST,
                    f"{name} palette[{index}] = {entry}",
                )
            if themed.cursor_color:
                self.assertGreaterEqual(
                    contrast_ratio(themed.cursor_color, background),
                    TERMINAL_MIN_CURSOR_CONTRAST,
                    name,
                )

    def test_signature_default_palette_has_no_pure_black(self):
        self.assertEqual(len(DEFAULT_PALETTE), 16)
        self.assertNotIn("#000000", DEFAULT_PALETTE)
        for entry in DEFAULT_PALETTE:
            self.assertGreaterEqual(
                contrast_ratio(entry, TerminalTheme().background_color),
                TERMINAL_MIN_PALETTE_CONTRAST,
                entry,
            )

    def test_terminator_black_import_is_lifted(self):
        imported = TerminalTheme(
            foreground_color="#ffffff",
            background_color="#000000",
            palette=(
                "#000000", "#aa0000", "#00aa00", "#aa5500",
                "#0000aa", "#aa00aa", "#00aaaa", "#aaaaaa",
                "#555555", "#ff5555", "#55ff55", "#ffff55",
                "#5555ff", "#ff55ff", "#55ffff", "#ffffff",
            ),
        )
        guarded = ensure_legible_theme(imported)
        for entry in guarded.palette:
            self.assertGreaterEqual(
                contrast_ratio(entry, "#000000"),
                TERMINAL_MIN_PALETTE_CONTRAST,
                entry,
            )

    def test_one_dark_black_no_longer_matches_background(self):
        base = TerminalTheme()
        themed = ensure_legible_theme(apply_terminal_palette(base, "one-dark"))
        self.assertNotEqual(themed.palette[0], themed.background_color)
        self.assertGreaterEqual(
            contrast_ratio(themed.palette[0], themed.background_color),
            TERMINAL_MIN_PALETTE_CONTRAST,
        )


class RaiseContrastBehaviour(unittest.TestCase):
    def test_passing_colors_are_untouched(self):
        self.assertEqual(raise_contrast("#ffffff", "#000000", 4.5), "#ffffff")
        self.assertEqual(raise_contrast("#82aaff", "#0a0d13", 3.0), "#82aaff")

    def test_idempotent_after_lift(self):
        lifted = raise_contrast("#282c34", "#282c34", 3.0)
        self.assertEqual(raise_contrast(lifted, "#282c34", 3.0), lifted)

    def test_light_background_blends_toward_black(self):
        lifted = raise_contrast("#fafafa", "#f6f7f9", 4.5)
        self.assertGreaterEqual(contrast_ratio(lifted, "#f6f7f9"), 4.5)

    def test_fails_open_on_junk_input(self):
        self.assertEqual(raise_contrast("nonsense", "#000000", 3.0), "nonsense")
        self.assertEqual(raise_contrast("#123456", "junk", 3.0), "#123456")
        theme = TerminalTheme(background_color="not-a-color")
        self.assertIs(ensure_legible_theme(theme), theme)

    def test_no_change_returns_same_theme_object(self):
        theme = TerminalTheme(
            foreground_color="#ffffff",
            background_color="#000000",
            palette=tuple(["#bbbbbb"] * 16),
        )
        self.assertIs(ensure_legible_theme(theme), theme)


class DefaultsAndWiring(unittest.TestCase):
    def test_t1000_is_the_default_palette(self):
        self.assertEqual(AppSettings().terminal_palette, "t1000")
        self.assertEqual(normalise_terminal_palette(None), "t1000")
        self.assertEqual(normalise_terminal_palette("default"), "t1000")
        self.assertEqual(normalise_terminal_palette("unknown-pack"), "t1000")
        self.assertIn("t1000", terminal_palette_names())

    def test_apply_theme_routes_through_legibility_guard(self):
        from termdeck import gtk_app

        source = inspect.getsource(gtk_app.apply_theme)
        self.assertIn("ensure_legible_theme", source)


if __name__ == "__main__":
    unittest.main()
