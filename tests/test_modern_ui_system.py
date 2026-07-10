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

    def test_polished_terminal_surface_has_distinct_depth_and_focus_states(self) -> None:
        css = build_chrome_css("dark", "comfortable")
        for selector in (
            ".td-main-root",
            "notebook.td-terminal-notebook",
            "notebook.td-terminal-notebook tab:checked",
            "headerbar button.suggested-action.td-header-icon",
            ".td-section-description",
            "button:focus",
            "scrolledwindow",
        ):
            self.assertIn(selector, css)
        self.assertIn("margin: 10px 12px 12px 12px", css)
        self.assertIn("border-radius: 16px", css)
        self.assertIn("inset 0 -2px 0", css)
        self.assertIn('"td-terminal-notebook"', GTK_APP)
        self.assertIn('"td-main-root"', GTK_APP)

    def test_header_exposes_only_window_name_new_tab_and_overflow(self) -> None:
        self.assertIn('title = Gtk.Label(label=self.window_title', GTK_APP)
        self.assertIn("title.set_max_width_chars(28)", GTK_APP)
        self.assertIn('new_tab = _icon_button(', GTK_APP)
        self.assertIn("overflow_header_actions", GTK_APP)
        self.assertIn('("Search tabs", self.open_search_window)', GTK_APP)
        self.assertIn('("Command palette", self.open_command_palette)', GTK_APP)
        self.assertIn('Gtk.MenuButton(label="⋯")', GTK_APP)
        self.assertIn('set_name("More actions")', GTK_APP)
        self.assertIn('"td-header-overflow", "td-header-icon"', GTK_APP)
        self.assertNotIn("primary_header_actions", GTK_APP)
        self.assertNotIn('status_dot = Gtk.Label', GTK_APP)
        self.assertNotIn('subtitle = Gtk.Label(label="Directory tabs"', GTK_APP)
        self.assertNotIn('("Search", "system-search-symbolic"', GTK_APP)
        self.assertNotIn('("Palette", "edit-find-symbolic"', GTK_APP)

    def test_tabs_pane_has_modern_hierarchy_and_empty_state(self) -> None:
        self.assertIn('Gtk.Label(label="WINDOW"', GTK_APP)
        self.assertIn("self.count_label", GTK_APP)
        self.assertIn('"td-count-pill"', GTK_APP)
        self.assertIn('"td-session-selected"', GTK_APP)
        self.assertIn('"td-empty-state"', GTK_APP)
        self.assertIn('Gtk.Label(label="No tabs in this window"', GTK_APP)
        self.assertIn('"td-primary-action"', GTK_APP)
        self.assertIn('"td-danger-action"', GTK_APP)

    def test_session_card_reference_uses_supported_python_attribute(self) -> None:
        self.assertIn("check._termdeck_session_card = card", GTK_APP)
        self.assertIn('getattr(button, "_termdeck_session_card", None)', GTK_APP)
        self.assertNotIn('set_data("termdeck-session-card"', GTK_APP)
        self.assertNotIn('get_data("termdeck-session-card"', GTK_APP)


if __name__ == "__main__":
    unittest.main()
