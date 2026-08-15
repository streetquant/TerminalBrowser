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
        self.assertGreaterEqual(tokens.card_radius, 8)

    def test_css_has_lean_surfaces_and_interaction_states(self) -> None:
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
        # Focus states use outline rings; hovers animate with short transitions
        self.assertIn("outline:", css)
        self.assertIn("transition:", css)
        self.assertIn("min-height: 48px", css)
        self.assertNotIn("0 18px 48px", css)

    def test_polished_terminal_surface_has_distinct_depth_and_focus_states(self) -> None:
        css = build_chrome_css("dark", "comfortable")
        for selector in (
            ".td-main-root",
            "notebook.td-terminal-notebook",
            "notebook.td-terminal-notebook tab:checked",
            "headerbar button.td-new-tab.td-header-icon",
            ".td-section-description",
            "button:focus",
            "scrolledwindow",
            "scrollbar.td-terminal-scrollbar",
        ):
            self.assertIn(selector, css)
        # Terminal notebook is docked edge-to-edge; no floating card margins
        self.assertIn("margin: 0;", css)
        self.assertIn("border-radius: 8px", css)
        # Active tab uses a slim accent underline instead of box-shadow inset
        self.assertIn("vte-terminal {", css)
        self.assertIn('"td-terminal-notebook"', GTK_APP)
        self.assertIn('"td-main-root"', GTK_APP)
        self.assertIn('"td-terminal-scrollbar"', GTK_APP)
        self.assertIn("scrollbar.td-terminal-scrollbar trough", css)
        self.assertIn("scrollbar.td-terminal-scrollbar slider", css)
        self.assertIn("min-width: 14px", css)
        self.assertIn("min-height: 24px", css)

    def test_tab_labels_reserve_readable_width(self) -> None:
        self.assertIn("name.set_width_chars(min(20, max(4, len(record.name))))", GTK_APP)
        self.assertIn("name.set_max_width_chars(20)", GTK_APP)

    def test_header_exposes_window_name_permanent_new_tab_and_overflow(self) -> None:
        self.assertIn('title = Gtk.Label(label=self.window_title', GTK_APP)
        self.assertIn("title.set_max_width_chars(28)", GTK_APP)
        self.assertIn('header_new_tab = _icon_button(', GTK_APP)
        self.assertIn("self._header_new_tab = header_new_tab", GTK_APP)
        self.assertIn("header.pack_start(header_new_tab)", GTK_APP)
        self.assertIn("self.new_tab,", GTK_APP)
        self.assertIn("def new_window_direct", GTK_APP)
        self.assertIn('("New T1000 window", self.new_window)', GTK_APP)
        self.assertIn("overflow_header_actions", GTK_APP)
        self.assertIn('("Search tabs", self.open_search_window)', GTK_APP)
        self.assertIn('("Command palette", self.open_command_palette)', GTK_APP)
        self.assertIn('Gtk.MenuButton(label="⋯")', GTK_APP)
        self.assertIn('set_name("More actions")', GTK_APP)
        self.assertIn('"td-new-tab", "td-header-icon"', GTK_APP)
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
