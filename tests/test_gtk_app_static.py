import ast
import importlib
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GTK_APP = PROJECT_ROOT / "termdeck" / "gtk_app.py"


class GtkAppStaticTests(unittest.TestCase):
    def test_gi_versions_pin_gtk3_stack_before_import(self):
        """Unversioned Gdk defaults to 4.0 on modern distros and breaks Gtk 3.0."""
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn('gi.require_version("Gtk", "3.0")', source)
        self.assertIn('gi.require_version("Gdk", "3.0")', source)
        self.assertIn('gi.require_version("Vte", "2.91")', source)
        gdk_pin = source.index('gi.require_version("Gdk", "3.0")')
        gi_import = source.index("from gi.repository import")
        self.assertLess(gdk_pin, gi_import, "Gdk 3.0 must be required before gi.repository import")

    def test_relative_imported_names_exist_without_loading_gi(self):
        tree = ast.parse(GTK_APP.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or not node.module or not node.level:
                continue
            if node.module in {"codex_archive", "codex_auth", "command_palette", "command_render", "shortcuts", "session_store", "models", "master_tasks", "health", "workspace_recipe", "control_plan", "session_lifecycle", "session_search", "recovery_intent"}:
                module = importlib.import_module(f"termdeck.{node.module}")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    self.assertTrue(hasattr(module, alias.name), f"{node.module}.{alias.name} is imported by gtk_app.py but missing")

    def test_startup_avoids_archive_scan_and_schedules_payload_revival(self):
        source = GTK_APP.read_text(encoding="utf-8")
        reconcile_call = source.index("reconcile_store(\n            self.store")
        activation_end = source.index("    def _schedule_legacy_recovery", reconcile_call)
        activation_source = source[reconcile_call:activation_end]
        self.assertNotIn("_schedule_legacy_recovery", activation_source)
        revival_call = activation_source.index("payload_revival_timeout =")
        window_restore = source.index(
            'restore_groups = getattr(self.store, "windows_for_restore", None)',
            reconcile_call,
        )
        self.assertLess(reconcile_call, reconcile_call + revival_call)
        self.assertLess(reconcile_call + revival_call, window_restore)

    def test_live_automatic_restore_uses_readiness_barrier_before_attach(self):
        source = GTK_APP.read_text(encoding="utf-8")
        restore_start = source.index("    def restore_previous_tabs")
        restore_end = source.index("    def _schedule_restore_retry", restore_start)
        restore_source = source[restore_start:restore_end]
        self.assertIn("coordinator.submit(", restore_source)
        self.assertIn("known_live=known_live", restore_source)
        self.assertIn("known_pid=pids_by_id.get(record.id)", restore_source)
        self.assertIn("if known_live is not True:", restore_source)

    def test_termdeck_window_has_no_duplicate_method_definitions(self):
        tree = ast.parse(GTK_APP.read_text(encoding="utf-8"))
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TermDeckWindow"]
        self.assertEqual(len(classes), 1)
        method_names = [node.name for node in classes[0].body if isinstance(node, ast.FunctionDef)]
        duplicates = {name: count for name, count in Counter(method_names).items() if count > 1}
        self.assertEqual(duplicates, {})

    def test_master_window_has_browser_task_manager_controls(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("Gtk.SearchEntry", source)
        self.assertIn("capture_view", source)
        self.assertIn("send_text_selected", source)
        self.assertIn("send_enter_selected", source)
        self.assertIn("update_capture_preview", source)
        self.assertIn("remove_selected", source)
        self.assertIn("archive_selected", source)
        self.assertIn('("Remove", self.remove_selected)', source)

    def test_gui_exposes_only_named_windows_and_immediate_shell_tabs(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("class WindowNameDialog", source)
        self.assertIn("def rename_window", source)
        self.assertIn("def new_tab", source)
        self.assertIn("def new_directory_tab", source)
        self.assertIn("current_directory(record)", source)
        self.assertIn("directory_tab_name", source)
        self.assertNotIn("Gtk.FileChooserAction.SELECT_FOLDER", source)
        self.assertNotIn("Gtk.FileChooserDialog", source)
        self.assertNotIn("class CommandDialog", source)
        self.assertNotIn("open_detected_project", source)
        self.assertNotIn("open_launcher", source)

    def test_windows_recover_from_removed_monitor_layouts(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn('self.connect("map", self._on_map_recover_offscreen)', source)
        self.assertIn("def _on_map_recover_offscreen", source)
        self.assertIn("self.move(target_x, target_y)", source)

    def test_every_window_keeps_a_permanent_new_tab_control(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn('header_new_tab = _icon_button(\n            "New tab"', source)
        self.assertIn("self.new_tab,", source)
        self.assertIn("self._header_new_tab = header_new_tab", source)
        self.assertIn("header.pack_start(header_new_tab)", source)
        self.assertNotIn("self._header_new_window", source)

    def test_gui_exposes_verified_per_terminal_codex_account_switching(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("Gtk.ComboBoxText", source)
        self.assertIn("CodexAuthManager", source)
        self.assertIn("_poll_codex_auth_state", source)
        self.assertIn("_complete_auth_switch", source)
        self.assertIn("threading.Thread", source)
        self.assertIn("_begin_auth_switch", source)
        self.assertIn("execute_auth_change", source)
        self.assertIn("exact Codex conversation resumed in this tab", source)
        self.assertIn("auth_restart_required\": not activation_ok", source)
        self.assertIn('self.notebook.connect("switch-page", self._on_notebook_switch_page)', source)
        self.assertIn('"Codex: external tmux"', source)
        self.assertIn("record.external", source)
        self.assertIn("record.id in self._auth_switch_inflight", source)
        self.assertIn('"Accounts: optional"', source)
        self.assertIn("Enable private Codex account integration", source)
        self.assertIn("ensure_auth_scope", source)
        self.assertIn("get_codex_auth_manager", source)
        self.assertIn("a running external tmux environment cannot be retroactively isolated", source)
        self.assertIn("private_auth_environment_ready", source)
        self.assertIn('"Codex: choose account · upgrades terminal"', source)
        self.assertIn("canonical_binding_ready", source)
        self.assertIn('"Codex: repair saved account binding"', source)
        self.assertIn("T1000 will preserve an exact Codex resume or a verified idle shell", source)

    def test_window_overview_is_exposed_in_gui(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("WindowOverview", source)
        self.assertIn("open_window_overview", source)
        self.assertIn("window_overview", source)
        self.assertIn('("Window overview · window totals", self.open_window_overview)', source)


    def test_shortcut_help_is_exposed_in_gui(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("ShortcutWindow", source)
        self.assertIn("open_shortcut_window", source)
        self.assertIn('("Keyboard shortcuts", self.open_shortcut_window)', source)
        self.assertIn("shortcut_entries", source)

    def test_shortcut_window_supports_close_and_column_sort(self):
        source = GTK_APP.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ShortcutWindow"]
        self.assertEqual(len(classes), 1)
        class_source = ast.get_source_segment(source, classes[0]) or ""
        self.assertIn("close_window", class_source)
        self.assertIn('label="Close"', class_source)
        self.assertIn("Gdk.KEY_Escape", class_source)
        self.assertIn("set_sort_column_id", class_source)
        self.assertIn("TreeModelSort", class_source)
        self.assertIn("set_headers_clickable", class_source)
        self.assertIn("_redistribute_column_widths", class_source)
        self.assertIn("toggle_fullscreen", class_source)
        self.assertIn("Gdk.WindowTypeHint.NORMAL", class_source)
        self.assertIn("set_hexpand", class_source)
        self.assertIn("set_vexpand", class_source)

    def test_session_search_is_exposed_in_gui(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("SessionSearchWindow", source)
        self.assertIn("open_search_window", source)
        self.assertIn("search_sessions", source)
        self.assertIn("Search", source)


    def test_shortcut_window_adds_outer_container_once(self):
        source = GTK_APP.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ShortcutWindow"]
        self.assertEqual(len(classes), 1)
        class_source = ast.get_source_segment(source, classes[0]) or ""
        self.assertEqual(class_source.count("self.add(outer)"), 1)

    def test_gui_bulk_controls_use_safety_plan(self):
        source = GTK_APP.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

        expected_methods = {
            "SidePane": {"send_prompt", "send_status", "pause_selected", "kill_selected"},
            "MasterWindow": {"send_to_selected", "status_selected", "pause_selected", "pause_hot", "kill_selected"},
            "TermDeckWindow": {"pause_hot_sessions"},
        }
        for class_name, method_names in expected_methods.items():
            methods = {node.name: node for node in classes[class_name].body if isinstance(node, ast.FunctionDef)}
            for method_name in method_names:
                method_source = ast.get_source_segment(source, methods[method_name]) or ""
                self.assertIn("confirm_control_plan", method_source, f"{class_name}.{method_name} bypasses the GUI safety plan")

    def test_command_palette_dispatch_exists(self):
        tree = ast.parse(GTK_APP.read_text(encoding="utf-8"))
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TermDeckWindow"]
        method_names = {node.name for node in classes[0].body if isinstance(node, ast.FunctionDef)}
        self.assertIn("open_command_palette", method_names)
        self.assertIn("execute_palette_item", method_names)
        self.assertIn("reopen_last_closed_tab", method_names)

    def test_reopen_closed_is_exposed_in_gui_controls(self):
        source = GTK_APP.read_text(encoding="utf-8")
        self.assertIn("reopen_last_closed_tab", source)
        self.assertIn("reopen_closed", source)
        self.assertIn("Reopen", source)


if __name__ == "__main__":
    unittest.main()
