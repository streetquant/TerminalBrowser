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
    def test_terminal_attach_waits_for_final_grid_and_seeds_stream_geometry(self) -> None:
        terminal = _section(GTK_APP, "class AttachTerminal", "class WindowOverview")
        self.assertIn('self.terminal.connect("size-allocate", self._on_terminal_size_allocate)', terminal)
        self.assertIn("GLib.timeout_add(60, self._connect_after_stable_allocation)", terminal)
        self.assertIn("self._connect_stream(columns=columns, rows=rows)", terminal)
        self.assertIn("def request_geometry_sync", terminal)
        self.assertNotIn("termios.TIOCSWINSZ", terminal)
        self.assertNotIn("_spawn_after_stable_allocation", terminal)

    def test_terminal_attach_probe_replies_route_to_controller_without_echo(self) -> None:
        terminal = _section(GTK_APP, "def _ensure_loopback_pty", "    def _connect_stream")
        self.assertLess(
            terminal.index("tty.setraw(slave_fd, when=termios.TCSANOW)"),
            terminal.index("self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)"),
        )
        self.assertLess(
            terminal.index("self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)"),
            terminal.index("self.terminal.set_pty(self.pty_obj)"),
        )
        self.assertIn("DA/XTVERSION probe replies", terminal)
        self.assertIn("self.controller.send_bytes(data)", terminal)
        self.assertNotIn("subprocess.Popen", terminal)

    def test_terminal_selection_syncs_to_external_clipboards_and_has_right_click_menu(self) -> None:
        terminal = _section(GTK_APP, "class AttachTerminal", "class WindowOverview")
        context_menu = _section(terminal, "def _show_terminal_context_menu", "def _on_button_press")
        button_press = _section(terminal, "def _on_button_press", "def _copy_selection_to_clipboards_idle")

        self.assertIn("Gtk.Clipboard.get_for_display", terminal)
        self.assertIn("clipboard.request_text", terminal)
        self.assertNotIn("wait_for_text", terminal)
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

    def test_main_tab_labels_have_browser_like_right_click_menu(self) -> None:
        main_window = _section(GTK_APP, "class TermDeckWindow", "class TermDeckApplication")
        self.assertIn("Right-click for tab actions", main_window)
        self.assertIn("def _tab_menu_item", main_window)
        self.assertIn("def _show_tab_context_menu", main_window)
        self.assertIn('self._tab_menu_item("Rename", lambda: self.rename_session(record))', main_window)
        self.assertNotIn('self._tab_menu_item("Retag"', main_window)
        self.assertIn('pin_label = "Unpin tab" if record.pinned else "Pin tab"', main_window)
        self.assertIn('close.connect("button-press-event", self._on_tab_label_press, record.id)', main_window)
        self.assertIn("def _hide_tab_from_close_button", main_window)
        self.assertIn("self.hide_tab(session_id, include_pinned=True)", main_window)
        self.assertIn("Ctrl+W · session keeps running", main_window)
        self.assertIn("close.get_accessible().set_name", main_window)
        self.assertIn("def _unpin_and_hide_tab", main_window)
        self.assertIn("self.hide_tab(record.id, include_pinned=True)", main_window)
        self.assertIn("Unpinned and hid tab", main_window)
        self.assertIn("def _visible_tab_records", main_window)
        self.assertIn("def _hide_tab_records", main_window)
        self.assertIn("def _hide_other_tabs", main_window)
        self.assertIn("def _hide_tabs_to_right", main_window)
        self.assertIn('self._tab_menu_item("Hide tab", lambda: self.hide_tab(record.id))', main_window)
        self.assertIn('self._tab_menu_item("Unpin and hide tab", lambda: self._unpin_and_hide_tab(record))', main_window)
        self.assertIn('self._tab_menu_item("Hide other tabs", lambda: self._hide_other_tabs(record))', main_window)
        self.assertIn('self._tab_menu_item("Hide tabs to the right", lambda: self._hide_tabs_to_right(record))', main_window)
        self.assertIn('candidate.tab_index > record.tab_index', main_window)
        self.assertIn('self._tab_menu_item("Kill tab", lambda: self.stop_session(record))', main_window)
        self.assertIn('self._tab_menu_item("Terminal options", self.open_terminal_options)', main_window)
        self.assertIn('self._tab_menu_item("Tab manager", self.open_master_window)', main_window)
        self.assertIn('self.notebook.connect("page-reordered", self._on_page_reordered)', main_window)
        self.assertIn("self.notebook.set_tab_reorderable(terminal, True)", main_window)
        self.assertIn("def _on_page_reordered", main_window)
        self.assertIn("self._renumber_visible_tabs()", main_window)
        self.assertIn("button == 3 and record is not None", main_window)
        self.assertIn("self._show_tab_context_menu(record, event)", main_window)

    def test_terminal_options_cover_cursor_blink_and_tabs_pane_side(self) -> None:
        options = _section(GTK_APP, "class TerminalOptionsDialog", "class ActionPaletteDialog")

        self.assertIn('self.cursor_blink = Gtk.CheckButton(label=\"Blink cursor\")', options)
        self.assertIn('self.side_position_combo = Gtk.ComboBoxText()', options)
        self.assertIn('\"cursor_blink\": bool(self.cursor_blink.get_active())', options)
        self.assertIn('\"side_pane_position\": self.side_position_combo.get_active_text() or \"right\"', options)
        self.assertIn("set_cursor_blink_mode", GTK_APP)
        self.assertIn("terminal_cursor_blink", SETTINGS)
        self.assertIn("side_pane_position", SETTINGS)
        self.assertIn("normalise_side_pane_position", SETTINGS)

    def test_tabs_pane_hides_killed_tabs_and_supports_ergonomic_multi_select(self) -> None:
        side = _section(GTK_APP, "class SidePane", "class MasterWindow")

        self.assertIn('title_event.connect(\"button-press-event\", self._on_header_button_press)', side)
        self.assertIn("def _on_header_button_press", side)
        self.assertIn("button == 3", side)
        self.assertIn("Move tabs pane to left", side)
        self.assertIn("Move tabs pane to right", side)
        self.assertIn("SIDE_PANE_HIDDEN_STATUSES", GTK_APP)
        self.assertIn('"killed"', GTK_APP)
        self.assertIn('"terminated"', GTK_APP)
        self.assertIn("_tabs_pane_records(", side)
        self.assertIn("self.selected_ids.intersection_update", side)
        self.assertIn("state & Gdk.ModifierType.CONTROL_MASK", side)
        self.assertIn("check.set_active(active)", side)
        self.assertIn("Ctrl+left-click any tab card to select multiple tabs", side)
        self.assertIn("if button == 1 and state & Gdk.ModifierType.CONTROL_MASK", side)
        self.assertNotIn("def _select_record_for_context", side)
        self.assertIn('check.connect("button-press-event", self._on_session_button_press, record, check)', side)
        self.assertIn('rename.connect("button-press-event", self._on_session_button_press, record, check)', side)
        self.assertIn('focus.connect("button-press-event", self._on_session_button_press, record, check)', side)
        self.assertIn("def _context_records", side)
        self.assertIn("def _context_label", side)
        self.assertIn('return f"{action} selected ({len(records)})" if len(records) > 1 else action', side)
        self.assertIn("def _hide_records", side)
        self.assertIn("def _pin_records", side)
        self.assertIn("def _unpin_records", side)
        self.assertNotIn('self._context_label("Retag", records)', side)
        self.assertIn('self._context_label("Pin", records)', side)
        self.assertIn('self._context_label("Unpin", records)', side)
        self.assertIn('self._context_label("Hide", records)', side)
        self.assertIn('self._context_label("Kill", records)', side)
        self.assertNotIn("lambda: self.window.retag_sessions(records)", side)
        self.assertIn("lambda: self._kill_records(records)", side)

    def test_terminal_history_ownership_cannot_regress_to_native_vte_scrollbar(self) -> None:
        terminal = _section(GTK_APP, "class AttachTerminal", "class WindowOverview")
        wheel = _section(terminal, "def _on_terminal_scroll", "# ------------------------------------------------------------------ #\n    # Scroll-idle auto-resume")
        thumb = _section(terminal, "def _on_history_scrollbar_value_changed", "def _feed_text")

        self.assertIn("self.native_adjustment = self.terminal.get_vadjustment()", terminal)
        self.assertIn("self.history_adjustment = Gtk.Adjustment.new(", terminal)
        self.assertNotIn("self.history_adjustment = self.terminal.get_vadjustment()", terminal)
        self.assertNotIn("_scrollbar_at_live_edge", terminal)
        self.assertNotIn("def _move_scrollbar", terminal)
        self.assertIn("def _move_native_history", terminal)
        self.assertIn("outcome.reached_live_edge", wheel)
        self.assertNotIn("get_vadjustment", wheel)
        self.assertIn("self._queue_history_scroll_delta(delta)", thumb)
        self.assertNotIn("self._scroll_handler(", thumb)

    def test_history_worker_never_resolves_rows_through_gtk_owned_sqlite(self) -> None:
        main_window = _section(GTK_APP, "class TermDeckWindow", "class TermDeckApplication")
        open_session = _section(main_window, "    def open_session", "    def _focus_session")
        scroll_binding = _section(
            open_session,
            "# Scrollbar drags execute on the bounded history worker",
            "selection_history_handler=",
        )

        self.assertIn("scroll_record=record", scroll_binding)
        self.assertIn("self._scroll_terminal(", scroll_binding)
        self.assertIn("scroll_record", scroll_binding)
        self.assertNotIn("current_callback_record", scroll_binding)
        self.assertNotIn("app_context.store", scroll_binding)
        self.assertIn("worker deliberately does not call this SQLite-backed helper", open_session)

    def test_terminal_history_live_edge_is_backend_evidence_not_scrollbar_geometry(self) -> None:
        terminal = _section(GTK_APP, "class AttachTerminal", "class WindowOverview")
        wheel = _section(terminal, "def _on_terminal_scroll", "# ------------------------------------------------------------------ #\n    # Scroll-idle auto-resume")

        self.assertIn("if outcome.reached_live_edge:", wheel)
        self.assertIn("self.prepare_for_live_input()", wheel)
        self.assertNotIn("line_delta > 0 and", wheel)
        self.assertNotIn("_history_bottom_value()", wheel)
        self.assertNotIn("_native_bottom_value()", wheel)

    def test_heavy_search_filters_debounce_refresh_work(self) -> None:
        self.assertIn("def _connect_debounced_refresh", GTK_APP)
        self.assertIn("GLib.timeout_add(delay_ms, _run)", GTK_APP)
        self.assertIn('widget.connect(\"search-changed\", _queue)', GTK_APP)
        self.assertIn('widget.connect(\"destroy\", _cleanup)', GTK_APP)
        self.assertEqual(GTK_APP.count("        _connect_debounced_refresh("), 4)
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
