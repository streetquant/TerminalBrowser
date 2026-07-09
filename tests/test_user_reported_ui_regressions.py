from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
GTK_APP = (REPO_ROOT / "termdeck" / "gtk_app.py").read_text()
SETTINGS = (REPO_ROOT / "termdeck" / "settings.py").read_text()


def _section(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


class UserReportedUiRegressionTests(unittest.TestCase):
    def test_terminal_selection_syncs_to_external_clipboards_and_has_right_click_menu(self) -> None:
        terminal = _section(GTK_APP, "class AttachTerminal", "class WindowOverview")
        context_menu = _section(terminal, "def _show_terminal_context_menu", "def _on_button_press")
        button_press = _section(terminal, "def _on_button_press", "def _copy_selection_to_clipboards_idle")

        self.assertIn("Gtk.Clipboard.get_for_display", terminal)
        self.assertIn("Gdk.SELECTION_CLIPBOARD", terminal)
        self.assertIn("Gdk.SELECTION_PRIMARY", terminal)
        self.assertIn("self._last_copied_text", terminal)
        self.assertIn("_copy_selection_to_clipboards_idle", terminal)
        self.assertIn('(\"Copy selection\", self.copy_clipboard)', context_menu)
        self.assertIn('(\"Paste\", self.paste_clipboard)', context_menu)
        self.assertIn('(\"Paste primary\", self.paste_primary)', context_menu)
        self.assertIn('(\"Select all\", lambda: self.terminal.select_all())', context_menu)
        self.assertIn('(\"Clear selection\", lambda: self.terminal.unselect_all())', context_menu)
        self.assertIn("button == 3", button_press)
        self.assertIn("self._show_terminal_context_menu(event)", button_press)

    def test_terminal_options_cover_cursor_blink_and_sessions_pane_side(self) -> None:
        options = _section(GTK_APP, "class TerminalOptionsDialog", "class ActionPaletteDialog")

        self.assertIn('self.cursor_blink = Gtk.CheckButton(label=\"Blink cursor\")', options)
        self.assertIn('self.side_position_combo = Gtk.ComboBoxText()', options)
        self.assertIn('\"cursor_blink\": bool(self.cursor_blink.get_active())', options)
        self.assertIn('\"side_pane_position\": self.side_position_combo.get_active_text() or \"right\"', options)
        self.assertIn("set_cursor_blink_mode", GTK_APP)
        self.assertIn("terminal_cursor_blink", SETTINGS)
        self.assertIn("side_pane_position", SETTINGS)
        self.assertIn("normalise_side_pane_position", SETTINGS)

    def test_sessions_pane_hides_killed_tabs_and_supports_ergonomic_multi_select(self) -> None:
        side = _section(GTK_APP, "class SidePane", "class MasterWindow")

        self.assertIn('title_event.connect(\"button-press-event\", self._on_header_button_press)', side)
        self.assertIn("def _on_header_button_press", side)
        self.assertIn("button == 3", side)
        self.assertIn("Move sessions pane to left", side)
        self.assertIn("Move sessions pane to right", side)
        self.assertIn('record.status != \"stopped\"', side)
        self.assertIn("self.selected_ids.intersection_update", side)
        self.assertIn("state & Gdk.ModifierType.CONTROL_MASK", side)
        self.assertIn("check.set_active(active)", side)
        self.assertIn("Ctrl-click a tag to multi-select", side)

    def test_heavy_search_filters_debounce_refresh_work(self) -> None:
        self.assertIn("def _connect_debounced_refresh", GTK_APP)
        self.assertIn("GLib.timeout_add(delay_ms, _run)", GTK_APP)
        self.assertIn('widget.connect(\"search-changed\", _queue)', GTK_APP)
        self.assertIn('widget.connect(\"destroy\", _cleanup)', GTK_APP)
        self.assertEqual(GTK_APP.count("        _connect_debounced_refresh("), 5)
        self.assertNotIn('connect(\"search-changed\", lambda *_: self.refresh())', GTK_APP)
        self.assertNotIn('connect(\"search-changed\", lambda *_: self._refresh_templates())', GTK_APP)

    def test_master_capture_preview_uses_short_lived_cache(self) -> None:
        master = _section(GTK_APP, "class MasterWindow", "class TermDeckWindow")
        self.assertIn("self._capture_preview_cache", master)
        self.assertIn("self._capture_preview_bucket_seconds", master)
        self.assertIn("def _capture_preview_cache_key", master)
        self.assertIn("time.monotonic() // self._capture_preview_bucket_seconds", master)
        self.assertIn("def capture_preview_text", master)
        self.assertIn("cached = self._capture_preview_cache.get(key)", master)
        self.assertIn("return cached", master)
        self.assertIn("backend.capture(record, lines=80)", master)
        self.assertIn("self._capture_preview_cache = {key: capture}", master)
        self.assertIn("self.set_capture_text(self.capture_preview_text(record))", master)

    def test_master_window_is_closable_and_refresh_work_is_teardown_safe(self) -> None:
        master = _section(GTK_APP, "class MasterWindow", "class TermDeckWindow")
        main_window = _section(GTK_APP, "class TermDeckWindow", "class TermDeckApplication")
        side = _section(GTK_APP, "class SidePane", "class MasterWindow")

        self.assertIn("_wire_auxiliary_close(self)", master)
        self.assertIn('close = Gtk.Button(label=\"Close\")', master)
        self.assertIn('close.connect(\"clicked\", lambda *_: self.destroy())', master)
        self.assertIn('self.connect(\"destroy\", lambda *_: setattr(self, \"_refresh_alive\", False))', master)
        self.assertIn('if not getattr(self, \"_refresh_alive\", False):', master)
        self.assertIn("return False", master)
        self.assertIn("GLib.timeout_add_seconds", master)
        self.assertIn("ui_refresh_interval_seconds", master)
        self.assertIn('self.master_window.connect(\"destroy\", lambda *_: setattr(self, \"master_window\", None))', main_window)
        self.assertIn('self.window_overview.connect(\"destroy\", lambda *_: setattr(self, \"window_overview\", None))', main_window)
        self.assertIn('self.search_window.connect(\"destroy\", lambda *_: setattr(self, \"search_window\", None))', main_window)
        self.assertIn("get_realized() and self.master_window.get_visible()", main_window)
        self.assertIn("get_realized() and self.window_overview.get_visible()", main_window)
        self.assertIn("get_realized() and self.search_window.get_visible()", main_window)
        self.assertIn("_last_refresh_signature", side)
        self.assertIn("time.monotonic() // self._refresh_bucket_seconds", side)
        self.assertIn("if signature == self._last_refresh_signature:", side)


if __name__ == "__main__":
    unittest.main()
