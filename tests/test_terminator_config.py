from pathlib import Path
import unittest

from termdeck.terminator_config import TerminatorConfig


SAMPLE = '''
[global_config]
[keybindings]
  new_tab = <Primary><Shift>t
  close_term = <Primary><Shift>w
[profiles]
  [[default]]
    font = JetBrains Mono 11
    use_system_font = False
    foreground_color = "#eeeeec"
    background_color = "#101010"
    cursor_color = "#ffcc00"
    cursor_shape = ibeam
    scrollback_lines = 24000
    palette = "#000000:#cc0000:#4e9a06:#c4a000:#3465a4:#75507b:#06989a:#d3d7cf:#555753:#ef2929:#8ae234:#fce94f:#729fcf:#ad7fa8:#34e2e2:#eeeeec"
[layouts]
  [[default]]
    [[[window0]]]
      type = Window
      size = 1440, 900
'''


class TerminatorConfigTests(unittest.TestCase):
    def test_nested_profile_and_layout_parse(self):
        config = TerminatorConfig.parse(SAMPLE)
        theme = config.to_theme()
        self.assertEqual(theme.font, "JetBrains Mono 11")
        self.assertFalse(theme.use_system_font)
        self.assertEqual(theme.foreground_color, "#eeeeec")
        self.assertEqual(theme.background_color, "#101010")
        self.assertEqual(theme.cursor_color, "#ffcc00")
        self.assertEqual(theme.cursor_shape, "ibeam")
        self.assertEqual(theme.scrollback_lines, 24000)
        self.assertEqual(theme.window_width, 1440)
        self.assertEqual(theme.window_height, 900)
        self.assertEqual(len(theme.palette), 16)
        self.assertEqual(theme.keybindings["new_tab"], "<Primary><Shift>t")

    def test_missing_profile_uses_defaults(self):
        config = TerminatorConfig.parse("[global_config]\n")
        theme = config.to_theme()
        self.assertEqual(theme.font, "Monospace 12")
        self.assertGreaterEqual(theme.scrollback_lines, 10000)

    def test_layout_converts_terminal_nodes_to_workspace_tabs(self):
        config = TerminatorConfig.parse(
            '\n[layouts]\n  [[default]]\n    [[[window0]]]\n      type = Window\n      title = Main Work\n    [[[child1]]]\n      type = VPaned\n      parent = window0\n    [[[terminal1]]]\n      type = Terminal\n      parent = child1\n      profile = default\n      title = agent\n      custom_command = codex\n      use_custom_command = True\n      directory = /repo\n    [[[terminal2]]]\n      type = Terminal\n      parent = child1\n      title = logs\n      command = tail -f app.log\n  [[ops]]\n    [[[window0]]]\n      type = Window\n    [[[terminal0]]]\n      type = Terminal\n      parent = window0\n      custom_command = htop\n      use_custom_command = True\n'
        )
        recipe = config.to_workspace_dict("default")
        self.assertEqual(recipe["name"], "terminator-default")
        self.assertEqual(recipe["windows"][0]["title"], "Main Work")
        tabs = recipe["windows"][0]["tabs"]
        self.assertEqual([tab["name"] for tab in tabs], ["agent", "logs"])
        self.assertEqual(tabs[0]["command"], "codex")
        self.assertEqual(tabs[0]["cwd"], "/repo")
        self.assertEqual(tabs[1]["command"], "tail -f app.log")
        self.assertEqual(config.layout_names(), ["default", "ops"])

    def test_layout_without_terminal_nodes_creates_shell_tab(self):
        config = TerminatorConfig.parse("""
[layouts]
  [[default]]
    [[[window0]]]
      type = Window
""")
        recipe = config.to_workspace_dict()
        self.assertEqual(recipe["windows"][0]["tabs"][0]["command"], "{shell}")


if __name__ == "__main__":
    unittest.main()
