from pathlib import Path
import unittest

from termdeck.ui_theme import build_chrome_css, resolve_chrome_tokens


ROOT = Path(__file__).resolve().parents[1]
GTK_APP = (ROOT / "termdeck" / "gtk_app.py").read_text(encoding="utf-8")


class ModernUiSystemTests(unittest.TestCase):
    def test_dark_tokens_use_layered_modern_palette(self) -> None:
        tokens = resolve_chrome_tokens("dark")
        self.assertNotEqual(tokens.bg, tokens.bg_raised)
        self.assertNotEqual(tokens.bg_raised, tokens.bg_sunken)
        self.assertGreaterEqual(tokens.card_radius, 12)

    def test_css_has_elevated_surfaces_and_interaction_states(self) -> None:
        css = build_chrome_css("dark", "comfortable")
        for selector in (
            ".td-session-card.td-session-selected",
            ".td-primary-action",
            ".td-ghost-action",
            ".td-danger-action",
            ".td-empty-state",
            ".td-count-pill",
            ".td-header-overflow",
        ):
            self.assertIn(selector, css)
        self.assertIn("box-shadow:", css)
        self.assertIn("transition:", css)
        self.assertIn("min-height: 52px", css)

    def test_header_uses_primary_actions_and_overflow_menu(self) -> None:
        self.assertIn("primary_header_actions", GTK_APP)
        self.assertIn("overflow_header_actions", GTK_APP)
        self.assertIn('Gtk.MenuButton(label="More ▾")', GTK_APP)
        self.assertIn('"td-header-overflow"', GTK_APP)
        self.assertNotIn("discover_group + manage_group + window_group", GTK_APP)

    def test_sessions_pane_has_modern_hierarchy_and_empty_state(self) -> None:
        self.assertIn('Gtk.Label(label="WORKSPACE"', GTK_APP)
        self.assertIn("self.count_label", GTK_APP)
        self.assertIn('"td-count-pill"', GTK_APP)
        self.assertIn('"td-session-selected"', GTK_APP)
        self.assertIn('"td-empty-state"', GTK_APP)
        self.assertIn('Gtk.Label(label="No active sessions"', GTK_APP)
        self.assertIn('"td-primary-action"', GTK_APP)
        self.assertIn('"td-danger-action"', GTK_APP)


if __name__ == "__main__":
    unittest.main()
