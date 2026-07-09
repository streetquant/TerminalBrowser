import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.cli import main


class CliDesktopTests(unittest.TestCase):
    def test_install_desktop_dry_run_outputs_json(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            code = main(["install-desktop", "--dry-run", "--data-home", tmp, "--command", "termdeck"])
            self.assertEqual(code, 0)
            written = "".join(call.args[0] + (call.kwargs.get("end", "\n")) for call in stdout.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["action"], "install-desktop")
            self.assertTrue(payload["dry_run"])
            self.assertIn("Exec=termdeck gui", payload["desktop_entry"])

    def test_install_and_uninstall_desktop_cli(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            self.assertEqual(main(["install-desktop", "--data-home", tmp, "--command", "termdeck"]), 0)
            desktop_file = Path(tmp) / "applications" / "io.github.termdeck.TermDeck.desktop"
            icon_file = Path(tmp) / "icons" / "hicolor" / "scalable" / "apps" / "termdeck.svg"
            self.assertTrue(desktop_file.exists())
            self.assertTrue(icon_file.exists())
            self.assertEqual(main(["uninstall-desktop", "--data-home", tmp]), 0)
            self.assertFalse(desktop_file.exists())
            self.assertFalse(icon_file.exists())


if __name__ == "__main__":
    unittest.main()
