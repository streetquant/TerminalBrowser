import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.shutdown_service import (
    SYSTEM_UNIT_NAME,
    USER_UNIT_NAME,
    install_system_checkpoint_service,
    install_user_checkpoint_service,
    render_system_checkpoint_service,
    render_user_checkpoint_service,
    uninstall_user_checkpoint_service,
)


class ShutdownServiceTests(unittest.TestCase):
    def test_rendered_unit_checkpoints_all_tabs_on_user_manager_exit(self):
        unit = render_user_checkpoint_service("/home/test/.local/bin/t1000")

        self.assertIn("Before=exit.target", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn(
            'ExecStop=-"/home/test/.local/bin/t1000" checkpoint --all',
            unit,
        )
        self.assertIn("TimeoutStopSec=15min", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_system_unit_stops_before_user_manager_and_mounts(self):
        unit = render_system_checkpoint_service(
            "/home/arch/.local/bin/t1000",
            user="max",
            uid=1000,
            home="/home/arch",
            required_mounts=("/8tb", "/storage"),
        )

        self.assertIn("After=user@1000.service local-fs.target", unit)
        self.assertIn('RequiresMountsFor="/home/arch" "/8tb" "/storage"', unit)
        self.assertIn(
            'ExecStop=-/usr/bin/runuser -u max -- /usr/bin/env HOME="/home/arch"',
            unit,
        )
        self.assertIn('PATH="/home/arch/.local/bin:', unit)
        self.assertIn('"/home/arch/.local/bin/t1000" checkpoint --all', unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_install_system_unit_requires_root_and_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "t1000"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            unit_dir = root / "systemd"

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                result = install_system_checkpoint_service(
                    executable=executable,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=("/8tb",),
                    unit_dir=unit_dir,
                    enable=True,
                )

            path = unit_dir / SYSTEM_UNIT_NAME
            content = path.read_text(encoding="utf-8")

        self.assertEqual(result.path, path)
        self.assertTrue(result.enabled)
        self.assertIn("After=user@1000.service", content)
        self.assertEqual(
            systemctl.call_args_list,
            [
                mock.call(["daemon-reload"]),
                mock.call(["enable", "--now", SYSTEM_UNIT_NAME]),
            ],
        )

    def test_install_writes_unit_atomically_and_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "t1000"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            unit_dir = root / "systemd"

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                result = install_user_checkpoint_service(
                    executable=executable,
                    unit_dir=unit_dir,
                    enable=True,
                )

            path = unit_dir / USER_UNIT_NAME
            content = path.read_text(encoding="utf-8")

        self.assertEqual(result.path, path)
        self.assertTrue(result.enabled)
        self.assertIn(str(executable.resolve()), content)
        self.assertEqual(
            systemctl.call_args_list,
            [
                mock.call(["daemon-reload"]),
                mock.call(["enable", "--now", USER_UNIT_NAME]),
            ],
        )

    def test_uninstall_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / USER_UNIT_NAME
            path.write_text("unit\n", encoding="utf-8")

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                result = uninstall_user_checkpoint_service(
                    unit_dir=root,
                    disable=True,
                )

            self.assertFalse(path.exists())
            self.assertFalse(result.enabled)
            self.assertEqual(
                systemctl.call_args_list,
                [
                    mock.call(["disable", "--now", USER_UNIT_NAME]),
                    mock.call(["daemon-reload"]),
                ],
            )


if __name__ == "__main__":
    unittest.main()
