import unittest

from termdeck.shortcuts import (
    build_shortcut_report,
    pause_shortcut_for_index,
    render_shortcut_table,
)


class ShortcutManifestTests(unittest.TestCase):
    def test_report_contains_terminal_conflicts_and_generated_pause_helper(self):
        report = build_shortcut_report()
        actions = {row["action"]: row for row in report["shortcuts"]}
        self.assertEqual(actions["close_term"]["terminal_conflict"], "shell/readline backward kill word")
        self.assertEqual(actions["close_term"]["accelerator"], "<Primary>w")
        self.assertEqual(actions["close_term"]["label"], "Close current tab, keep process alive")
        self.assertEqual(actions["quit_app"]["accelerator"], "<Primary>q")
        self.assertEqual(actions["quit_app"]["label"], "Quit t1000, keep all processes alive")
        self.assertEqual(actions["reopen_closed"]["accelerator"], "<Primary><Shift>t")
        self.assertNotIn("task_launcher", actions)
        self.assertNotIn("new_command", actions)
        self.assertEqual(pause_shortcut_for_index(3), "Ctrl+Alt+3")
        self.assertEqual(pause_shortcut_for_index(10), "")

    def test_terminator_keybinding_overrides_supported_browser_actions(self):
        report = build_shortcut_report({"new_tab": "<Alt>t", "copy": "<Primary><Shift>c"})
        actions = {row["action"]: row for row in report["shortcuts"]}
        self.assertEqual(actions["new_tab"]["accelerator"], "<Alt>t")
        self.assertEqual(actions["new_tab"]["source"], "terminator")

    def test_terminator_import_cannot_displace_ctrl_shift_v_paste(self):
        report = build_shortcut_report({"paste": "<Alt>v"})
        actions = {row["action"]: row for row in report["shortcuts"]}
        self.assertEqual(actions["paste"]["accelerator"], "<Primary><Shift>v")
        self.assertEqual(actions["paste"]["source"], "default")

    def test_emacs_safe_profile_rejects_terminal_conflicting_terminator_binding(self):
        report = build_shortcut_report({"close_term": "<Primary>w"}, profile="emacs-safe")
        actions = {row["action"]: row for row in report["shortcuts"]}
        self.assertNotEqual(actions["close_term"]["accelerator"], "<Primary>w")
        self.assertIn("conflicts", actions["close_term"]["note"])

    def test_render_shortcut_table_is_plain_text(self):
        table = render_shortcut_table(build_shortcut_report())
        self.assertIn("Action", table)
        self.assertIn("New shell tab", table)


if __name__ == "__main__":
    unittest.main()
