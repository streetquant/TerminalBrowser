"""WCAG AA contrast guarantees for every chrome token pack.

These tests are the enforcement half of the design-token system: any new or
edited theme must keep text pairs at >= 4.5:1 and semantic hues at >= 3:1, so
a color tweak can never silently ship an unreadable shell.
"""

import unittest

from termdeck.ui_theme import (
    CHROME_THEME_NAMES,
    CHROME_TOKENS,
    MIN_TEXT_CONTRAST,
    MIN_UI_CONTRAST,
    build_chrome_css,
    contrast_ratio,
    contrast_violations,
    effective_chrome_theme,
    normalise_chrome_theme,
    relative_luminance,
    resolve_chrome_tokens,
)


class ContrastMathTests(unittest.TestCase):
    def test_luminance_extremes(self) -> None:
        self.assertAlmostEqual(relative_luminance("#000000"), 0.0, places=6)
        self.assertAlmostEqual(relative_luminance("#ffffff"), 1.0, places=6)

    def test_contrast_ratio_extremes_and_symmetry(self) -> None:
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#ffffff", "#000000"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#808080", "#808080"), 1.0, places=6)

    def test_short_hex_and_invalid_input(self) -> None:
        self.assertAlmostEqual(relative_luminance("#fff"), 1.0, places=6)
        with self.assertRaises(ValueError):
            relative_luminance("not-a-color")

    def test_thresholds_match_wcag_aa(self) -> None:
        self.assertEqual(MIN_TEXT_CONTRAST, 4.5)
        self.assertEqual(MIN_UI_CONTRAST, 3.0)


class ChromeTokenContrastTests(unittest.TestCase):
    def test_every_theme_name_has_a_token_pack(self) -> None:
        for name in CHROME_THEME_NAMES:
            self.assertIn(name, CHROME_TOKENS)

    def test_all_token_packs_are_wcag_aa_clean(self) -> None:
        for name, tokens in CHROME_TOKENS.items():
            with self.subTest(theme=name):
                self.assertEqual(contrast_violations(tokens), [])

    def test_violations_are_reported_with_ratios(self) -> None:
        from dataclasses import replace

        broken = replace(CHROME_TOKENS["dark"], fg="#20242e", name="broken")
        failures = contrast_violations(broken)
        self.assertTrue(failures)
        self.assertTrue(any("broken: fg on bg is" in line for line in failures))


class LightThemeTests(unittest.TestCase):
    def test_light_theme_is_first_class(self) -> None:
        self.assertIn("light", CHROME_THEME_NAMES)
        self.assertEqual(normalise_chrome_theme("light"), "light")
        self.assertEqual(resolve_chrome_tokens("light").name, "light")
        self.assertEqual(effective_chrome_theme("light"), "light")

    def test_light_aliases_normalise(self) -> None:
        self.assertEqual(normalise_chrome_theme("day"), "light")
        self.assertEqual(normalise_chrome_theme("paper"), "light")

    def test_light_chrome_css_builds(self) -> None:
        css = build_chrome_css("light", "comfortable")
        self.assertTrue(css)
        self.assertIn(CHROME_TOKENS["light"].bg, css)

    def test_system_pack_mirrors_light_pack(self) -> None:
        light = CHROME_TOKENS["light"]
        system = CHROME_TOKENS["system"]
        for field in ("bg", "bg_raised", "bg_sunken", "fg", "fg_muted", "border",
                      "accent", "accent_fg", "danger", "warning", "success"):
            self.assertEqual(getattr(light, field), getattr(system, field), field)


if __name__ == "__main__":
    unittest.main()
