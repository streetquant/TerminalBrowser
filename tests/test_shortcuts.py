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

    def test_emacs_safe_profile_rejects_terminal_conflicting_terminator_binding(self):
        report = build_shortcut_report({"close_term": "<Primary>w"}, profile="emacs-safe")
        actions = {row["action"]: row for row in report["shortcuts"]}
        self.assertNotEqual(actions["close_term"]["accelerator"], "<Primary>w")
        self.assertIn("conflicts", actions["close_term"]["note"])

    def test_render_shortcut_table_is_plain_text(self):
        table = render_shortcut_table(build_shortcut_report())
        self.assertIn("Action", table)
        self.assertIn("New directory tab", table)


if __name__ == "__main__":
    unittest.main()
