import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.cli import cmd_quickstart
from termdeck.quickstart import build_quickstart


TERMINATOR_CONFIG = """
[global_config]
[keybindings]
  new_tab = <Primary>t
[profiles]
  [[default]]
    font = Fira Code 11
    foreground_color = #eeeeec
    background_color = #101010
    scrollback_lines = 7777
[layouts]
  [[default]]
    [[[window0]]]
      type = Window
      title = Agents
    [[[terminal0]]]
      type = Terminal
      parent = window0
      title = codex
      custom_command = codex
      use_custom_command = True
"""


class QuickstartTests(unittest.TestCase):
    def test_quickstart_combines_dependencies_directory_terminator_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo-app"
            root.mkdir()
            config_path = Path(tmp) / "terminator.conf"
            config_path.write_text(TERMINATOR_CONFIG, encoding="utf-8")
            data_home = Path(tmp) / "xdg"

            with mock.patch("termdeck.quickstart.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
                "termdeck.quickstart._has_gi_namespace", return_value=True
            ):
                payload = build_quickstart(
                    root,
                    terminator_path=config_path,
                    data_home=data_home,
                    desktop_command="termdeck",
                )

            self.assertEqual(payload["schema"], "termdeck.quickstart")
            self.assertTrue(payload["ready_for_gui"])
            self.assertEqual(payload["directory"]["path"], str(root.resolve()))
            self.assertEqual(payload["directory"]["default_tab_name"], "demo-app")
            self.assertTrue(payload["terminator"]["found"])
            self.assertEqual(payload["terminator"]["font"], "Fira Code 11")
            self.assertFalse(payload["desktop"]["installed"])
            self.assertNotIn("project", payload)
            action_ids = {item["id"] for item in payload["recommended_actions"]}
            self.assertIn("open-directory-tab", action_ids)
            self.assertIn("inspect-terminator-theme", action_ids)
            self.assertIn("install-app-launcher", action_ids)
            self.assertIn("launch-gui", action_ids)

    def test_missing_runtime_dependency_adds_install_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("termdeck.quickstart.shutil.which", return_value=None), mock.patch(
                "termdeck.quickstart._has_gi_namespace", return_value=False
            ):
                payload = build_quickstart(root)
            self.assertFalse(payload["ready_for_gui"])
            action_ids = [item["id"] for item in payload["recommended_actions"]]
            self.assertEqual(action_ids[0], "install-dependencies")

    def test_cli_quickstart_outputs_directory_model_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backend"
            root.mkdir()
            args = argparse.Namespace(
                path=root,
                terminator_config=None,
                data_home=Path(tmp) / "xdg",
                desktop_command="termdeck",
            )
            with mock.patch("termdeck.quickstart.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
                "termdeck.quickstart._has_gi_namespace", return_value=True
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = cmd_quickstart(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], "termdeck.quickstart")
            self.assertEqual(payload["directory"]["default_tab_name"], "backend")
            self.assertNotIn("project", payload)


if __name__ == "__main__":
    unittest.main()
