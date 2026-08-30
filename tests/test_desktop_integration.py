import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck import desktop_integration
from termdeck.desktop_integration import (
    APP_ID,
    DesktopIntegrationError,
    MANAGED_LAUNCHER,
    build_autostart_entry,
    build_desktop_entry,
    build_install_plan,
    desktop_paths,
    install_desktop,
    managed_autostart_ready,
    resolve_launch_command,
    uninstall_desktop,
)


class DesktopIntegrationTests(unittest.TestCase):
    def test_paths_follow_xdg_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = desktop_paths(tmp)
            self.assertEqual(paths.desktop_file, Path(tmp) / "applications" / f"{APP_ID}.desktop")
            self.assertEqual(paths.icon_file, Path(tmp) / "icons" / "hicolor" / "scalable" / "apps" / "t1000.svg")
            self.assertEqual(paths.autostart_file, paths.config_home / "autostart" / f"{APP_ID}.desktop")

    def test_resolve_command_prefers_explicit_override_and_adds_gui(self):
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck"), "/opt/termdeck/bin/termdeck gui")
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck gui"), "/opt/termdeck/bin/termdeck gui")

    def test_resolve_command_falls_back_to_python_module(self):
        with mock.patch("termdeck.desktop_integration.shutil.which", return_value=None), mock.patch("termdeck.desktop_integration.sys.executable", "/usr/bin/python3"):
            self.assertEqual(resolve_launch_command(), "/usr/bin/python3 -m termdeck gui")

    def test_desktop_entry_contains_browser_like_metadata(self):
        entry = build_desktop_entry("t1000 gui")
        self.assertIn("Name=T1000", entry)
        self.assertIn("Exec=t1000 gui", entry)
        self.assertIn("Icon=t1000", entry)
        self.assertIn("Terminal=false", entry)
        self.assertIn("Categories=System;TerminalEmulator;Development;Utility;", entry)
        self.assertTrue(entry.endswith("\n"))

    def test_autostart_entry_uses_only_managed_launcher(self):
        entry = build_autostart_entry()
        self.assertIn("Exec=/home/arch/.local/bin/t1000 gui", entry)
        self.assertIn("TryExec=/home/arch/.local/bin/t1000", entry)
        self.assertIn("X-T1000-Storage-Fabric=warp-drives-v1", entry)
        self.assertIn("X-T1000-Fail-Closed=true", entry)
        self.assertNotIn("sh -c", entry)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS=", entry)
        self.assertNotIn("DISPLAY=", entry)

    def test_autostart_entry_uses_the_plan_command_exactly(self):
        plan = build_install_plan(command="/opt/custom/t1000 --profile desktop")
        self.assertIn(f"Exec={plan.command}", plan.autostart_entry)
        self.assertIn("TryExec=/opt/custom/t1000", plan.autostart_entry)
        self.assertIn("X-T1000-Managed-Launcher=/opt/custom/t1000", plan.autostart_entry)
        self.assertNotIn("/home/arch/.local/bin/t1000", plan.autostart_entry)

    def test_autostart_path_follows_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = desktop_paths(config_home=tmp)
            self.assertEqual(paths.autostart_dir, Path(tmp) / "autostart")
            self.assertEqual(paths.autostart_file, Path(tmp) / "autostart" / f"{APP_ID}.desktop")

    def test_managed_autostart_readiness_fails_closed_for_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready, detail = managed_autostart_ready(
                launcher=Path(tmp) / "missing-launcher",
                managed_root=Path(tmp) / "missing-root" / "t1000-managed",
            )
            self.assertFalse(ready)
            self.assertIn("unavailable", detail)

    def test_install_and_uninstall_write_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp, config_home=tmp)
            payload = install_desktop(plan)
            self.assertTrue(payload["ok"])
            self.assertTrue(plan.paths.desktop_file.exists())
            self.assertTrue(plan.paths.icon_file.exists())
            self.assertIn("Exec=t1000 gui", plan.paths.desktop_file.read_text())
            self.assertIn("<svg", plan.paths.icon_file.read_text())
            removed = uninstall_desktop(data_home=tmp)
            self.assertEqual(set(removed["removed"]), {str(plan.paths.desktop_file), str(plan.paths.icon_file)})
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())

    def test_install_autostart_requires_verified_managed_ssd(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(False, "storage unavailable"),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp, config_home=tmp)
            payload = install_desktop(plan, install_autostart=True)
            self.assertFalse(payload["ok"])
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.autostart_file.exists())
            self.assertIn("refusing autostart", payload["error"])

    def test_install_and_remove_autostart(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(True, "verified"),
        ):
            config = Path(tmp) / "config"
            plan = build_install_plan(
                command=str(MANAGED_LAUNCHER), data_home=tmp, config_home=config
            )
            payload = install_desktop(plan, install_autostart=True)
            self.assertTrue(payload["ok"])
            self.assertTrue(plan.paths.autostart_file.exists())
            self.assertEqual(plan.paths.autostart_file.stat().st_mode & 0o777, 0o644)
            self.assertIn("Exec=/home/arch/.local/bin/t1000 gui", plan.paths.autostart_file.read_text())
            removed = uninstall_desktop(config_home=config, data_home=tmp, remove_autostart=True)
            self.assertIn(str(plan.paths.autostart_file), removed["removed"])
            self.assertFalse(plan.paths.autostart_file.exists())

    def test_autostart_readiness_checks_the_plan_launcher(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(True, "verified"),
        ) as readiness:
            plan = build_install_plan(command="/opt/custom/t1000", data_home=tmp)
            install_desktop(plan, install_autostart=True)
            readiness.assert_called_once_with(launcher=Path("/opt/custom/t1000"))

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            payload = install_desktop(plan, dry_run=True)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertIn("desktop_entry", payload)

    def test_install_rejects_symlink_and_non_regular_targets_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            outside = Path(tmp) / "outside"
            outside.write_text("keep", encoding="utf-8")
            plan.paths.applications_dir.mkdir(parents=True)
            plan.paths.desktop_file.symlink_to(outside)
            with self.assertRaisesRegex(DesktopIntegrationError, "regular file"):
                install_desktop(plan)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertFalse(plan.paths.icon_file.exists())

            plan.paths.desktop_file.unlink()
            plan.paths.desktop_file.mkdir()
            with self.assertRaisesRegex(DesktopIntegrationError, "regular file"):
                install_desktop(plan)
            self.assertFalse(plan.paths.icon_file.exists())

    def test_install_rejects_symlinked_parent_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            plan = build_install_plan(command="t1000", data_home=root / "data")
            plan.paths.data_home.mkdir()
            plan.paths.applications_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(DesktopIntegrationError, "parent"):
                install_desktop(plan)
            self.assertEqual(list(outside.iterdir()), [])

    def test_install_failure_restores_preimages_without_backup_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            plan.paths.applications_dir.mkdir(parents=True)
            plan.paths.icon_dir.mkdir(parents=True)
            plan.paths.desktop_file.write_text("old desktop", encoding="utf-8")
            plan.paths.icon_file.write_text("old icon", encoding="utf-8")
            original_desktop = plan.paths.desktop_file.read_bytes()
            original_icon = plan.paths.icon_file.read_bytes()
            original_write = desktop_integration._atomic_write_bytes
            failed = False

            def fail_icon(path, content, *, mode):
                nonlocal failed
                if Path(path) == plan.paths.icon_file and not failed:
                    failed = True
                    raise OSError("simulated icon publication failure")
                return original_write(path, content, mode=mode)

            with mock.patch(
                "termdeck.desktop_integration._atomic_write_bytes",
                side_effect=fail_icon,
            ), self.assertRaisesRegex(OSError, "simulated icon publication failure"):
                install_desktop(plan)

            self.assertEqual(plan.paths.desktop_file.read_bytes(), original_desktop)
            self.assertEqual(plan.paths.icon_file.read_bytes(), original_icon)
            self.assertEqual(list(plan.paths.applications_dir.glob(".*.tmp-*")), [])
            self.assertEqual(list(plan.paths.icon_dir.glob(".*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
