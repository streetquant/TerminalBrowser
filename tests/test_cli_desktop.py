import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.cli import main


class CliDesktopTests(unittest.TestCase):
    def test_install_desktop_dry_run_outputs_json(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            code = main(["install-desktop", "--dry-run", "--data-home", tmp, "--command", "t1000"])
            self.assertEqual(code, 0)
            written = "".join(call.args[0] + (call.kwargs.get("end", "\n")) for call in stdout.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["action"], "install-desktop")
            self.assertTrue(payload["dry_run"])
            self.assertIn("Exec=t1000 gui", payload["desktop_entry"])

    def test_install_desktop_autostart_requires_managed_ssd(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"), mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(False, "storage unavailable"),
        ):
            self.assertEqual(
                main(
                    [
                        "install-desktop",
                        "--autostart",
                        "--data-home",
                        tmp,
                        "--config-home",
                        tmp,
                    ]
                ),
                1,
            )

    def test_install_and_uninstall_desktop_cli(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            self.assertEqual(main(["install-desktop", "--data-home", tmp, "--command", "t1000"]), 0)
            desktop_file = Path(tmp) / "applications" / "io.github.t1000.T1000.desktop"
            icon_file = Path(tmp) / "icons" / "hicolor" / "scalable" / "apps" / "t1000.svg"
            self.assertTrue(desktop_file.exists())
            self.assertTrue(icon_file.exists())
            self.assertEqual(main(["uninstall-desktop", "--data-home", tmp]), 0)
            self.assertFalse(desktop_file.exists())
            self.assertFalse(icon_file.exists())

    def test_cli_removes_autostart_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"), mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(True, "verified"),
        ):
            self.assertEqual(
                main(
                    [
                        "install-desktop",
                        "--autostart",
                        "--data-home",
                        tmp,
                        "--config-home",
                        tmp,
                    ]
                ),
                0,
            )
            autostart = Path(tmp) / "autostart" / "io.github.t1000.T1000.desktop"
            self.assertTrue(autostart.exists())
            self.assertEqual(
                main(
                    [
                        "uninstall-desktop",
                        "--autostart",
                        "--data-home",
                        tmp,
                        "--config-home",
                        tmp,
                    ]
                ),
                0,
            )
            self.assertFalse(autostart.exists())


if __name__ == "__main__":
    unittest.main()
