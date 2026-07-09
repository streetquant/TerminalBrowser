import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.desktop_integration import (
    APP_ID,
    build_desktop_entry,
    build_install_plan,
    desktop_paths,
    install_desktop,
    resolve_launch_command,
    uninstall_desktop,
)


class DesktopIntegrationTests(unittest.TestCase):
    def test_paths_follow_xdg_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = desktop_paths(tmp)
            self.assertEqual(paths.desktop_file, Path(tmp) / "applications" / f"{APP_ID}.desktop")
            self.assertEqual(paths.icon_file, Path(tmp) / "icons" / "hicolor" / "scalable" / "apps" / "termdeck.svg")

    def test_resolve_command_prefers_explicit_override_and_adds_gui(self):
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck"), "/opt/termdeck/bin/termdeck gui")
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck gui"), "/opt/termdeck/bin/termdeck gui")

    def test_resolve_command_falls_back_to_python_module(self):
        with mock.patch("termdeck.desktop_integration.shutil.which", return_value=None), mock.patch("termdeck.desktop_integration.sys.executable", "/usr/bin/python3"):
            self.assertEqual(resolve_launch_command(), "/usr/bin/python3 -m termdeck gui")

    def test_desktop_entry_contains_browser_like_metadata(self):
        entry = build_desktop_entry("termdeck gui")
        self.assertIn("Name=TermDeck", entry)
        self.assertIn("Exec=termdeck gui", entry)
        self.assertIn("Icon=termdeck", entry)
        self.assertIn("Terminal=false", entry)
        self.assertIn("Categories=System;TerminalEmulator;Development;Utility;", entry)
        self.assertTrue(entry.endswith("\n"))

    def test_install_and_uninstall_write_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="termdeck", data_home=tmp)
            payload = install_desktop(plan)
            self.assertTrue(payload["ok"])
            self.assertTrue(plan.paths.desktop_file.exists())
            self.assertTrue(plan.paths.icon_file.exists())
            self.assertIn("Exec=termdeck gui", plan.paths.desktop_file.read_text())
            self.assertIn("<svg", plan.paths.icon_file.read_text())
            removed = uninstall_desktop(data_home=tmp)
            self.assertEqual(set(removed["removed"]), {str(plan.paths.desktop_file), str(plan.paths.icon_file)})
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="termdeck", data_home=tmp)
            payload = install_desktop(plan, dry_run=True)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertIn("desktop_entry", payload)


if __name__ == "__main__":
    unittest.main()
