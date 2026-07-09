import json
import tempfile
import unittest
from pathlib import Path

from termdeck.models import TerminalTheme
from termdeck.settings import AppSettings, sample_settings_json
from termdeck.ui_theme import (
    apply_terminal_palette,
    build_chrome_css,
    chrome_theme_names,
    density_names,
    format_resource_cue,
    health_badge_class,
    kind_for_session,
    kind_glyph,
    normalise_chrome_theme,
    normalise_density,
    normalise_terminal_palette,
    parse_font_description,
    resolve_chrome_tokens,
    scale_font_description,
    terminal_palette_names,
    terminal_palette_pack,
    toast_message_for_action,
)


class UiThemeTests(unittest.TestCase):
    def test_chrome_themes_emit_non_empty_css(self):
        for name in chrome_theme_names():
            css = build_chrome_css(name, "comfortable")
            self.assertTrue(css.strip())
            self.assertIn("TermDeck chrome", css)
            self.assertIn(".td-session-card", css)
            self.assertIn("headerbar", css)
            tokens = resolve_chrome_tokens("dark" if name == "system" else name)
            self.assertTrue(tokens.bg.startswith("#"))
        # Explicit packs stay distinct from each other.
        self.assertNotEqual(resolve_chrome_tokens("dark").bg, resolve_chrome_tokens("system").bg)
        self.assertNotEqual(resolve_chrome_tokens("dark").bg, resolve_chrome_tokens("noir").bg)

    def test_chrome_css_uses_readable_font_sizes(self):
        css = build_chrome_css("dark", "comfortable")
        # Guard against shipping UI that is hard to read (pre-fix was 10–11px).
        self.assertIn("font-size: 14px", css)
        self.assertIn("font-size: 15px", css)
        self.assertNotIn("font-size: 10px", css)
        self.assertNotIn("font-size: 11px", css)

    def test_ui_font_options_affect_chrome_css(self):
        default_css = build_chrome_css("dark", "comfortable")
        custom = build_chrome_css("dark", "comfortable", ui_font="Inter 12", ui_font_scale=1.2)
        self.assertIn("Inter", custom)
        self.assertIn("ui_font=Inter 12", custom)
        self.assertNotEqual(default_css, custom)
        # Scaled base should exceed the unscaled 12pt ladder.
        self.assertIn("font-size: 14.4px", custom)

    def test_system_theme_follows_desktop_preference(self):
        from termdeck import ui_theme
        from termdeck.ui_theme import effective_chrome_theme

        original = ui_theme.detect_system_prefers_dark
        try:
            ui_theme.detect_system_prefers_dark = lambda: False  # type: ignore[assignment]
            self.assertEqual(effective_chrome_theme("system"), "system")
            light_css = build_chrome_css("system", "comfortable")
            dark_css = build_chrome_css("dark", "comfortable")
            self.assertNotEqual(light_css, dark_css)
            self.assertIn(resolve_chrome_tokens("system").bg, light_css)
            self.assertIn("theme=system", light_css)
            self.assertNotIn(resolve_chrome_tokens("system").bg, dark_css)

            ui_theme.detect_system_prefers_dark = lambda: True  # type: ignore[assignment]
            self.assertEqual(effective_chrome_theme("system"), "dark")
            system_dark_css = build_chrome_css("system", "comfortable")
            self.assertEqual(system_dark_css, dark_css)
            self.assertIn("theme=dark", system_dark_css)
        finally:
            ui_theme.detect_system_prefers_dark = original  # type: ignore[assignment]

    def test_density_changes_css_payload(self):
        cozy = build_chrome_css("dark", "comfortable")
        compact = build_chrome_css("dark", "compact")
        self.assertNotEqual(cozy, compact)
        self.assertIn("td-density-compact", compact)

    def test_terminal_palette_presets_apply(self):
        base = TerminalTheme()
        self.assertEqual(apply_terminal_palette(base, "terminator").background_color, base.background_color)
        mocha = apply_terminal_palette(base, "catppuccin")
        pack = terminal_palette_pack("catppuccin")
        self.assertEqual(mocha.background_color, pack["background"])
        self.assertEqual(mocha.foreground_color, pack["foreground"])
        self.assertEqual(len(mocha.palette), 16)
        for name in terminal_palette_names():
            themed = apply_terminal_palette(base, name)
            self.assertEqual(len(themed.palette), 16)

    def test_kind_and_badge_helpers(self):
        self.assertEqual(kind_for_session(command="npm run dev"), "node")
        self.assertEqual(kind_for_session(command="codex"), "ai")
        self.assertTrue(kind_glyph("docker"))
        self.assertEqual(health_badge_class("critical cpu"), "td-badge-danger")
        self.assertEqual(health_badge_class("ok"), "td-badge-ok")
        self.assertIn("CPU", format_resource_cue(12.3, "64 MiB"))
        self.assertEqual(toast_message_for_action("paused", 2), "Paused 2 sessions")

    def test_settings_appearance_roundtrip(self):
        payload = json.loads(sample_settings_json())
        self.assertIn("appearance", payload)
        self.assertIn(payload["appearance"]["chrome_theme"], chrome_theme_names())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "appearance": {
                            "chrome_theme": "noir",
                            "terminal_palette": "tokyo-night",
                            "density": "compact",
                            "focus_mode": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = AppSettings.load(path)
            self.assertEqual(settings.chrome_theme, "noir")
            self.assertEqual(settings.terminal_palette, "tokyo-night")
            self.assertEqual(settings.ui_density, "compact")
            self.assertTrue(settings.focus_mode)
            summary = settings.budget_summary()
            self.assertEqual(summary["appearance"]["chrome_theme"], "noir")

    def test_legacy_settings_without_appearance_still_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"health": {"critical_cpu_percent": 111}}), encoding="utf-8")
            settings = AppSettings.load(path)
            self.assertEqual(settings.chrome_theme, "dark")
            self.assertEqual(settings.terminal_palette, "terminator")
            self.assertEqual(settings.ui_density, "comfortable")
            self.assertFalse(settings.focus_mode)

    def test_normalisers(self):
        self.assertEqual(normalise_chrome_theme("NOIR"), "noir")
        self.assertEqual(normalise_chrome_theme("nope"), "dark")
        self.assertEqual(normalise_density("dense"), "compact")
        self.assertEqual(normalise_terminal_palette("tokyonight"), "tokyo-night")
        self.assertEqual(set(density_names()), {"comfortable", "compact"})

    def test_font_zoom_helpers(self):
        family, size = parse_font_description("JetBrains Mono 12")
        self.assertEqual(family, "JetBrains Mono")
        self.assertEqual(size, 12.0)
        self.assertEqual(scale_font_description("Monospace 12", 1), "Monospace 13")
        self.assertEqual(scale_font_description("Monospace 12", -2), "Monospace 10")
        # Clamp to minimum readable size.
        self.assertEqual(scale_font_description("Monospace 7", -10), "Monospace 6")
        # Clamp to max.
        self.assertEqual(scale_font_description("Monospace 70", 10), "Monospace 72")


class UiSurfaceStaticTests(unittest.TestCase):
    """Structural checks for the ten high-impact UI surfaces in gtk_app."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.gtk = (root / "termdeck" / "gtk_app.py").read_text(encoding="utf-8")
        cls.palette = (root / "termdeck" / "command_palette.py").read_text(encoding="utf-8")
        cls.theme = (root / "termdeck" / "ui_theme.py").read_text(encoding="utf-8")

    def test_chrome_theme_system_present(self):
        self.assertIn("build_chrome_css", self.gtk)
        self.assertIn("apply_app_chrome", self.gtk)
        self.assertIn("chrome_theme", self.theme)
        for name in ("dark", "noir", "system"):
            self.assertIn(f'"{name}"', self.theme)

    def test_headerbar_grouped_icon_actions(self):
        self.assertIn("_icon_button", self.gtk)
        self.assertIn("create_group", self.gtk)
        self.assertIn("utilities-terminal-symbolic", self.gtk)
        self.assertIn("set_tooltip_text", self.gtk)

    def test_side_pane_session_cards(self):
        self.assertIn("td-session-card", self.gtk)
        self.assertIn("td-chip", self.gtk)
        self.assertIn("health_badge_class", self.gtk)
        self.assertIn("More actions", self.gtk)

    def test_notebook_tab_affordances(self):
        self.assertIn("_build_tab_label", self.gtk)
        self.assertIn("kind_glyph", self.gtk)
        self.assertIn("td-tab-close", self.gtk)

    def test_gui_rename_session_is_wired(self):
        self.assertIn("def rename_session", self.gtk)
        self.assertIn("rename_record", self.gtk)
        self.assertIn("Rename", self.gtk)
        self.assertIn("rename_current", self.palette)
        self.assertIn("DOUBLE_BUTTON_PRESS", self.gtk)
        self.assertIn("F2", self.gtk)

    def test_terminal_options_dialog_is_wired(self):
        self.assertIn("class TerminalOptionsDialog", self.gtk)
        self.assertIn("open_terminal_options", self.gtk)
        self.assertIn("apply_terminal_options", self.gtk)
        self.assertIn("terminal_options", self.palette)
        self.assertIn("cursor_shape", self.theme)
        self.assertIn("cursor_blink", self.gtk)
        self.assertIn("set_cursor_blink_mode", self.gtk)
        self.assertIn("CursorBlinkMode.OFF", self.gtk)
        self.assertIn("side_pane_position", self.gtk)
        self.assertIn("apply_terminal_overrides", self.theme)

    def test_terminal_font_zoom_shortcuts_are_wired(self):
        self.assertIn("handle_zoom_event", self.gtk)
        self.assertIn("on_terminal_key_press", self.gtk)
        self.assertIn("zoom_font", self.gtk)
        self.assertIn("font_zoom_delta", self.gtk)
        self.assertIn("KEY_KP_Add", self.gtk)
        self.assertIn("scale_font_description", self.gtk)
        shortcuts = (Path(__file__).resolve().parents[1] / "termdeck" / "shortcuts.py").read_text(encoding="utf-8")
        self.assertIn("zoom_in", shortcuts)
        self.assertIn("zoom_out", shortcuts)
        self.assertIn("<Primary>equal", shortcuts)
        self.assertIn("<Primary>minus", shortcuts)

    def test_terminal_select_to_copy_is_wired(self):
        self.assertIn("_on_button_release", self.gtk)
        self.assertIn("_copy_selection_to_clipboards", self.gtk)
        self.assertIn("get_has_selection", self.gtk)
        self.assertIn("button-release-event", self.gtk)
        self.assertIn("connect_after", self.gtk)
        self.assertIn("_queue_selection_copy", self.gtk)
        self.assertIn("timeout_add", self.gtk)
        self.assertIn("source_remove", self.gtk)
        self.assertIn("_selection_autocopy_source_id", self.gtk)
        self.assertIn("get_text_selected", self.gtk)
        self.assertIn("SELECTION_CLIPBOARD", self.gtk)
        self.assertIn("SELECTION_PRIMARY", self.gtk)
        self.assertIn("paste_primary", self.gtk)
        self.assertIn("_feed_text", self.gtk)
        self.assertIn("_last_copied_text", self.gtk)
        self.assertIn("_system_clipboard", self.gtk)
        self.assertIn("get_for_display", self.gtk)
        self.assertIn("_show_terminal_context_menu", self.gtk)
        self.assertIn("Select all", self.gtk)
        self.assertIn("_is_paste_event", self.gtk)
        self.assertIn("feed_child", self.gtk)

    def test_sessions_pane_ux_controls_are_wired(self):
        self.assertIn("_on_header_button_press", self.gtk)
        self.assertIn("Move sessions pane to left", self.gtk)
        self.assertIn("set_side_pane_position", self.gtk)
        self.assertIn("_on_tag_button_press", self.gtk)
        self.assertIn("CONTROL_MASK", self.gtk)
        self.assertIn("SIDE_PANE_HIDDEN_STATUSES", self.gtk)
        self.assertIn('"killed"', self.gtk)
        self.assertIn("_show_in_sessions_pane(record)", self.gtk)
        self.assertIn("_last_refresh_signature", self.gtk)
        self.assertIn("_refresh_bucket_seconds", self.gtk)
        self.assertIn("time.monotonic() // self._refresh_bucket_seconds", self.gtk)
        self.assertIn("ui_refresh_interval_seconds", self.gtk)
        self.assertIn("GLib.timeout_add_seconds(int(max(1, round(getattr", self.gtk)
        refresh_start = self.gtk.index("    def refresh(self) -> None:")
        cache_check = self.gtk.index("if signature == self._last_refresh_signature:", refresh_start)
        first_clear = self.gtk.index("for child in self.session_box.get_children():", refresh_start)
        self.assertLess(cache_check, first_clear)

    def test_auxiliary_windows_have_explicit_close_paths(self):
        self.assertIn("_wire_auxiliary_close", self.gtk)
        self.assertIn("KEY_Escape", self.gtk)
        self.assertIn('Gtk.Button(label="Close")', self.gtk)

    def test_refresh_timers_stop_after_destroy(self):
        self.assertIn("_refresh_alive", self.gtk)
        self.assertIn('connect("destroy", lambda *_: setattr(self, "_refresh_alive", False))', self.gtk)
        self.assertIn('if not getattr(self, "_refresh_alive", False):', self.gtk)
        self.assertIn("return False", self.gtk)

    def test_palette_and_launcher_polish(self):
        self.assertIn("td-overlay-dialog", self.gtk)
        self.assertIn("td-category", self.gtk)
        self.assertIn("td-list-row", self.gtk)
        self.assertIn("focus_mode", self.palette)

    def test_table_badge_and_density_hooks(self):
        self.assertIn("td-table-window", self.gtk)
        self.assertIn('set_property("family", "Monospace")', self.gtk)
        self.assertIn("td-density-", self.gtk)

    def test_terminal_presets_and_focus_toast(self):
        self.assertIn("apply_terminal_palette", self.gtk)
        self.assertIn("catppuccin", self.theme)
        self.assertIn("tokyo-night", self.theme)
        self.assertIn("toggle_focus_mode", self.gtk)
        self.assertIn("show_toast", self.gtk)
        self.assertIn("td-toast", self.gtk)
        self.assertIn("toast_message_for_action", self.gtk)


if __name__ == "__main__":
    unittest.main()
