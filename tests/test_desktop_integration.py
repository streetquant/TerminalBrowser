import errno
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck import desktop_integration
from termdeck.desktop_integration import (
    APP_ID,
    AUTOSTART_FILENAME,
    DESKTOP_DATABASE_TIMEOUT_SECONDS,
    DESKTOP_FILENAME,
    DesktopIntegrationError,
    MANAGED_LAUNCHER,
    MAX_DESKTOP_TARGET_BYTES,
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
    def _managed_readiness_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        storage_root = root / "storage"
        managed_root = storage_root / "t1000-managed"
        storage_root.mkdir()
        managed_root.mkdir()
        bootstrap = managed_root / "bootstrap.py"
        bootstrap.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        bootstrap.chmod(0o755)
        launcher = root / "t1000"
        launcher.write_text(
            f'#!/bin/sh\nset -eu\nexec /usr/bin/python3 -I -B {bootstrap} "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        state = {
            "launcher_path": str(launcher),
            "python_executable": "/usr/bin/python3",
            "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
            "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
        }
        state_path = managed_root / "state.json"
        state_path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        return launcher, managed_root, storage_root

    def _mock_ready_attestation(self):
        return mock.patch(
            "termdeck.storage_fabric.attest_storage_fabric",
            return_value=SimpleNamespace(ok=True, reason="verified"),
        )

    def test_paths_follow_xdg_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = desktop_paths(tmp)
            self.assertEqual(paths.desktop_file, Path(tmp) / "applications" / f"{APP_ID}.desktop")
            self.assertEqual(paths.icon_file, Path(tmp) / "icons" / "hicolor" / "scalable" / "apps" / "t1000.svg")
            self.assertEqual(paths.autostart_file, paths.config_home / "autostart" / f"{APP_ID}.desktop")

    def test_xdg_paths_reject_relative_values_and_empty_mapping_isolated(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/ambient/config"}):
            self.assertEqual(
                desktop_integration.xdg_config_home({}),
                Path.home() / ".config",
            )
        for name, function in (
            ("XDG_DATA_HOME", desktop_integration.xdg_data_home),
            ("XDG_CONFIG_HOME", desktop_integration.xdg_config_home),
        ):
            for value in ("relative/path", "~/.config-alt"):
                with self.subTest(name=name, value=value), self.assertRaises(
                    DesktopIntegrationError
                ):
                    function({name: value})

    def test_resolve_command_prefers_explicit_override_and_adds_gui(self):
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck"), "/opt/termdeck/bin/termdeck gui")
        self.assertEqual(resolve_launch_command("/opt/termdeck/bin/termdeck gui"), "/opt/termdeck/bin/termdeck gui")

    def test_resolve_command_falls_back_to_python_module(self):
        with mock.patch("termdeck.desktop_integration.shutil.which", return_value=None), mock.patch("termdeck.desktop_integration.sys.executable", "/usr/bin/python3"):
            self.assertEqual(resolve_launch_command(), "/usr/bin/python3 -m termdeck gui")

    def test_install_plan_rejects_unsafe_menu_command(self):
        for command in (
            "/usr/bin/t1000\nHidden=true",
            "'/usr/bin/t1000'",
            "\"/opt/T 1000/bin/t1000\"",
            "/opt/T\\ 1000/bin/t1000",
            "/usr/bin/t1000 %f",
        ):
            with self.subTest(command=command), self.assertRaises(DesktopIntegrationError):
                build_install_plan(command=command)

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
        self.assertIn("Exec=/warp/runtime/termdeck/bin/t1000 gui", entry)
        self.assertIn("TryExec=/warp/runtime/termdeck/bin/t1000", entry)
        self.assertIn("X-T1000-Storage-Policy=direct-root", entry)
        self.assertIn("X-T1000-Fail-Closed=true", entry)
        self.assertNotIn("sh -c", entry)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS=", entry)
        self.assertNotIn("DISPLAY=", entry)

    def test_autostart_entry_ignores_menu_override_and_uses_stable_launcher(self):
        plan = build_install_plan(command="/opt/custom/t1000 --profile desktop")
        self.assertIn("Exec=/warp/runtime/termdeck/bin/t1000 gui", plan.autostart_entry)
        self.assertIn("TryExec=/warp/runtime/termdeck/bin/t1000", plan.autostart_entry)
        self.assertIn(
            "X-T1000-Managed-Launcher=/warp/runtime/termdeck/bin/t1000",
            plan.autostart_entry,
        )
        self.assertNotIn("/opt/custom/t1000", plan.autostart_entry)
        self.assertEqual(plan.as_dict()["managed_launcher"], str(MANAGED_LAUNCHER))

    def test_autostart_template_rejects_duplicate_or_conflicting_exec(self):
        resource = desktop_integration.resources.files("termdeck").joinpath(
            "data",
            desktop_integration.AUTOSTART_RESOURCE,
        )
        poisoned = resource.read_text(encoding="utf-8") + "Exec=/opt/custom/t1000 gui\n"
        with mock.patch(
            "termdeck.desktop_integration.resources.files"
        ) as files, self.assertRaises(DesktopIntegrationError):
            files.return_value.joinpath.return_value.read_text.return_value = poisoned
            build_autostart_entry()

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
            self.assertIn("failed closed", detail)

    def test_managed_autostart_readiness_accepts_owned_private_paths(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            ready, detail = managed_autostart_ready(
                launcher=launcher,
                managed_root=managed_root,
            )
            self.assertTrue(ready, detail)

    def test_managed_autostart_readiness_rejects_foreign_owner(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            with mock.patch(
                "termdeck.desktop_integration.os.geteuid",
                return_value=os.geteuid() + 1,
            ):
                ready, detail = managed_autostart_ready(
                    launcher=launcher,
                    managed_root=managed_root,
                )
            self.assertFalse(ready)
            self.assertIn("not owned", detail)

    def test_managed_autostart_readiness_rejects_group_writable_managed_paths(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, storage_root = self._managed_readiness_fixture(Path(tmp))
            paths = {
                "launcher": launcher,
                "managed root": managed_root,
                "storage root": storage_root,
                "bootstrap": managed_root / "bootstrap.py",
                "release state": managed_root / "state.json",
            }
            for label, path in paths.items():
                with self.subTest(path=label):
                    original_mode = stat.S_IMODE(path.stat().st_mode)
                    path.chmod(original_mode | stat.S_IWGRP)
                    try:
                        ready, detail = managed_autostart_ready(
                            launcher=launcher,
                            managed_root=managed_root,
                        )
                    finally:
                        path.chmod(original_mode)
                    self.assertFalse(ready)
                    self.assertIn("group/world writable", detail)

    def test_managed_path_metadata_rejects_single_link_directory(self):
        metadata = os.stat_result(
            (
                stat.S_IFDIR | 0o700,
                1,
                1,
                1,
                os.geteuid(),
                os.getgid(),
                0,
                0,
                0,
                0,
            )
        )
        detail = desktop_integration._managed_path_metadata_error(
            Path("/managed"),
            metadata,
            directory=True,
        )
        self.assertIn("unsafe link count", detail)

    def test_managed_autostart_readiness_rejects_hard_linked_launcher_and_bootstrap(self):
        for target in ("launcher", "bootstrap", "release state"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
                launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
                selected = {
                    "launcher": launcher,
                    "bootstrap": managed_root / "bootstrap.py",
                    "release state": managed_root / "state.json",
                }[target]
                os.link(selected, Path(tmp) / f"{target}-link")
                ready, detail = managed_autostart_ready(
                    launcher=launcher,
                    managed_root=managed_root,
                )
                self.assertFalse(ready)
                self.assertIn("multiple hard links", detail)

    def test_managed_autostart_readiness_rejects_state_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            state_path = managed_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["launcher_sha256"] = "0" * 64
            state_path.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            ready, detail = managed_autostart_ready(
                launcher=launcher,
                managed_root=managed_root,
            )
            self.assertFalse(ready)
            self.assertIn("launcher_sha256", detail)

    def test_managed_autostart_readiness_rejects_launcher_path_in_a_comment(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            bootstrap = managed_root / "bootstrap.py"
            launcher.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                f"# intended bootstrap: {bootstrap}\n"
                'exec /bin/false -I -B /bin/false "$@"\n',
                encoding="utf-8",
            )
            ready, detail = managed_autostart_ready(
                launcher=launcher,
                managed_root=managed_root,
            )
            self.assertFalse(ready)
            self.assertIn("canonical", detail)

    def test_managed_autostart_readiness_rejects_crlf_launcher_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            launcher.write_bytes(launcher.read_bytes().replace(b"\n", b"\r\n"))
            ready, detail = managed_autostart_ready(
                launcher=launcher,
                managed_root=managed_root,
            )
            self.assertFalse(ready)
            self.assertIn("LF-only", detail)

    def test_managed_autostart_readiness_rejects_symlinked_intermediate_ancestors(self):
        for selected in ("launcher", "managed root"):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp, self._mock_ready_attestation():
                base = Path(tmp)
                real = base / "real"
                real.mkdir()
                launcher, managed_root, _storage_root = self._managed_readiness_fixture(real)
                alias = base / "alias"
                alias.symlink_to(real, target_is_directory=True)
                selected_launcher = alias / launcher.name if selected == "launcher" else launcher
                selected_root = (
                    alias / "storage" / "t1000-managed"
                    if selected == "managed root"
                    else managed_root
                )
                ready, detail = managed_autostart_ready(
                    launcher=selected_launcher,
                    managed_root=selected_root,
                )
                self.assertFalse(ready)
                self.assertIn("failed closed", detail)

    def test_managed_autostart_readiness_revalidates_launcher_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(Path(tmp))
            original = launcher.read_bytes()
            moved = Path(tmp) / "launcher-before-swap"

            def swap_launcher(**_kwargs):
                launcher.rename(moved)
                launcher.write_bytes(original)
                launcher.chmod(0o755)
                return SimpleNamespace(ok=True, reason="verified")

            with mock.patch(
                "termdeck.storage_fabric.attest_storage_fabric",
                side_effect=swap_launcher,
            ):
                ready, detail = managed_autostart_ready(
                    launcher=launcher,
                    managed_root=managed_root,
            )
            self.assertFalse(ready)
            self.assertIn("changed", detail)

    def test_no_nofollow_platform_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher, managed_root, _storage_root = self._managed_readiness_fixture(root)
            plan = build_install_plan(command="t1000", data_home=root / "data")
            with mock.patch.object(desktop_integration.os, "O_NOFOLLOW", 0):
                ready, detail = managed_autostart_ready(
                    launcher=launcher,
                    managed_root=managed_root,
                )
                self.assertFalse(ready)
                self.assertIn("O_NOFOLLOW", detail)
                with self.assertRaisesRegex(DesktopIntegrationError, "O_NOFOLLOW"):
                    install_desktop(plan)

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
            self.assertIn("Exec=/warp/runtime/termdeck/bin/t1000 gui", plan.paths.autostart_file.read_text())
            removed = uninstall_desktop(config_home=config, data_home=tmp, remove_autostart=True)
            self.assertIn(str(plan.paths.autostart_file), removed["removed"])
            self.assertFalse(plan.paths.autostart_file.exists())

    def test_autostart_readiness_checks_the_stable_managed_launcher(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.managed_autostart_ready",
            return_value=(True, "verified"),
        ) as readiness:
            plan = build_install_plan(
                command="/opt/custom/t1000",
                data_home=tmp,
                config_home=tmp,
            )
            install_desktop(plan, install_autostart=True)
            readiness.assert_called_once_with(launcher=MANAGED_LAUNCHER)

    def test_canonical_autostart_mutation_is_release_manager_owned(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(Path(tmp) / "config")},
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            installed = install_desktop(plan, install_autostart=True)
            self.assertFalse(installed["ok"])
            self.assertIn("release install/rollback receipts", installed["error"])
            self.assertFalse(plan.paths.autostart_file.exists())

            removed = uninstall_desktop(
                data_home=tmp,
                remove_autostart=True,
            )
            self.assertFalse(removed["ok"])
            self.assertIn("release install/rollback receipts", removed["error"])

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            payload = install_desktop(plan, dry_run=True)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertIn("desktop_entry", payload)

    def test_requested_desktop_database_failure_is_truthful_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.shutil.which",
            return_value="/usr/bin/update-desktop-database",
        ), mock.patch(
            "termdeck.desktop_integration.subprocess.run",
            return_value=SimpleNamespace(returncode=7),
        ) as run:
            plan = build_install_plan(command="t1000", data_home=tmp)
            payload = install_desktop(plan, update_database=True)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "desktop database update failed")
            self.assertEqual(payload["update_desktop_database"]["returncode"], 7)
            self.assertFalse(payload["update_desktop_database"]["timed_out"])
            self.assertEqual(payload["rollback"]["desktop_files"], "restored")
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(
                run.call_args.kwargs["timeout"],
                DESKTOP_DATABASE_TIMEOUT_SECONDS,
            )

    def test_requested_desktop_database_timeout_is_truthful(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.shutil.which",
            return_value="/usr/bin/update-desktop-database",
        ), mock.patch(
            "termdeck.desktop_integration.subprocess.run",
            side_effect=subprocess.TimeoutExpired("update-desktop-database", 15),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            payload = install_desktop(plan, update_database=True)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "desktop database update timed out")
            self.assertIsNone(payload["update_desktop_database"]["returncode"])
            self.assertTrue(payload["update_desktop_database"]["timed_out"])
            self.assertEqual(payload["rollback"]["desktop_files"], "restored")
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())

    def test_desktop_database_helper_disappearance_rolls_back_files(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.shutil.which",
            return_value="/usr/bin/update-desktop-database",
        ), mock.patch(
            "termdeck.desktop_integration.subprocess.run",
            side_effect=FileNotFoundError("helper disappeared"),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            payload = install_desktop(plan, update_database=True)
            self.assertFalse(payload["ok"])
            self.assertIn("disappeared", payload["error"])
            self.assertFalse(payload["update_desktop_database"]["executed"])
            self.assertEqual(payload["rollback"]["desktop_files"], "restored")
            self.assertEqual(payload["rollback"]["desktop_database"], "unchanged")
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())

    def test_desktop_database_failure_restores_existing_preimages(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration.shutil.which",
            return_value="/usr/bin/update-desktop-database",
        ), mock.patch(
            "termdeck.desktop_integration.subprocess.run",
            return_value=SimpleNamespace(returncode=9),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            plan.paths.applications_dir.mkdir(parents=True)
            plan.paths.icon_file.parent.mkdir(parents=True)
            plan.paths.desktop_file.write_bytes(b"previous desktop\n")
            plan.paths.icon_file.write_bytes(b"previous icon\n")
            plan.paths.desktop_file.chmod(0o640)
            plan.paths.icon_file.chmod(0o600)

            payload = install_desktop(plan, update_database=True)

            self.assertFalse(payload["ok"])
            self.assertEqual(plan.paths.desktop_file.read_bytes(), b"previous desktop\n")
            self.assertEqual(plan.paths.icon_file.read_bytes(), b"previous icon\n")
            self.assertEqual(stat.S_IMODE(plan.paths.desktop_file.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(plan.paths.icon_file.stat().st_mode), 0o600)

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

    def test_install_rejects_hard_linked_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            plan.paths.applications_dir.mkdir(parents=True)
            plan.paths.desktop_file.write_text("keep", encoding="utf-8")
            hard_link = Path(tmp) / "desktop-hard-link"
            os.link(plan.paths.desktop_file, hard_link)
            with self.assertRaisesRegex(DesktopIntegrationError, "multiple hard links"):
                install_desktop(plan)
            self.assertEqual(plan.paths.desktop_file.read_text(encoding="utf-8"), "keep")
            self.assertEqual(hard_link.read_text(encoding="utf-8"), "keep")

    def test_existing_target_must_be_owned_by_current_user(self):
        observed = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644,
            st_nlink=1,
            st_uid=os.geteuid() + 1,
        )
        with self.assertRaisesRegex(DesktopIntegrationError, "not owned"):
            desktop_integration._validate_destination_stat(
                Path("/managed.desktop"),
                observed,
            )

    def test_install_rejects_oversized_existing_target_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            plan.paths.applications_dir.mkdir(parents=True)
            oversized = b"x" * (MAX_DESKTOP_TARGET_BYTES + 1)
            plan.paths.desktop_file.write_bytes(oversized)
            with self.assertRaisesRegex(DesktopIntegrationError, "inspection limit"):
                install_desktop(plan)
            self.assertEqual(plan.paths.desktop_file.stat().st_size, len(oversized))
            self.assertFalse(plan.paths.icon_file.exists())

    def test_install_fails_closed_if_target_lock_times_out(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            desktop_integration,
            "DESKTOP_TARGET_LOCK_TIMEOUT_SECONDS",
            0.0,
        ), mock.patch(
            "termdeck.desktop_integration.fcntl.flock",
            side_effect=BlockingIOError(errno.EAGAIN, "busy"),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            with self.assertRaisesRegex(DesktopIntegrationError, "timed out waiting"):
                install_desktop(plan)
            self.assertFalse(plan.paths.desktop_file.exists())
            self.assertFalse(plan.paths.icon_file.exists())

    def test_install_fails_closed_without_atomic_noreplace_rename(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.desktop_integration._rename_noreplace_at",
            side_effect=DesktopIntegrationError("renameat2 unavailable"),
        ):
            plan = build_install_plan(command="t1000", data_home=tmp)
            with self.assertRaisesRegex(DesktopIntegrationError, "renameat2 unavailable"):
                install_desktop(plan)
            self.assertFalse(plan.paths.desktop_file.exists())
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

    def test_install_fails_closed_if_an_intermediate_ancestor_is_swapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_home = root / "data"
            outside = root / "outside"
            outside.mkdir()
            plan = build_install_plan(command="t1000", data_home=data_home)
            original_replace = desktop_integration.os.replace
            moved_data = root / "data-before-swap"
            swapped = False

            def swap_data_home_then_replace(
                source,
                destination,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
            ):
                nonlocal swapped
                if not swapped:
                    data_home.rename(moved_data)
                    data_home.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with mock.patch(
                "termdeck.desktop_integration.os.replace",
                side_effect=swap_data_home_then_replace,
            ), self.assertRaisesRegex(DesktopIntegrationError, "parent"):
                install_desktop(plan)

            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse(
                moved_data.joinpath("applications", DESKTOP_FILENAME).exists()
            )
            self.assertFalse(
                moved_data.joinpath(
                    "icons", "hicolor", "scalable", "apps", "t1000.svg"
                ).exists()
            )

    def test_failed_transaction_preserves_noncooperating_concurrent_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            original_write = desktop_integration._atomic_write_bytes
            concurrent_content = b"concurrent successful install\n"
            sabotaged = False

            def replace_before_failure(path, content, **kwargs):
                nonlocal sabotaged
                if Path(path) == plan.paths.icon_file and not sabotaged:
                    sabotaged = True
                    replacement = plan.paths.applications_dir / ".concurrent"
                    replacement.write_bytes(concurrent_content)
                    os.replace(replacement, plan.paths.desktop_file)
                    raise OSError("simulated later publication failure")
                return original_write(path, content, **kwargs)

            with mock.patch(
                "termdeck.desktop_integration._atomic_write_bytes",
                side_effect=replace_before_failure,
            ), self.assertRaisesRegex(DesktopIntegrationError, "rollback was incomplete"):
                install_desktop(plan)

            self.assertTrue(sabotaged)
            self.assertEqual(plan.paths.desktop_file.read_bytes(), concurrent_content)
            self.assertFalse(plan.paths.icon_file.exists())

    def test_rollback_quarantine_unlink_never_deletes_public_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            original_write = desktop_integration._atomic_write_bytes
            original_unlink = desktop_integration.os.unlink
            concurrent_content = b"concurrent successful install at rollback unlink\n"
            injected = False

            def fail_icon(path, content, **kwargs):
                if Path(path) == plan.paths.icon_file:
                    raise OSError("simulated later publication failure")
                return original_write(path, content, **kwargs)

            def replace_public_before_quarantine_unlink(name, *, dir_fd=None):
                nonlocal injected
                if ".rollback-" in name and "rollback-preimage" not in name and not injected:
                    replacement = plan.paths.applications_dir / ".concurrent-at-unlink"
                    replacement.write_bytes(concurrent_content)
                    os.replace(replacement, plan.paths.desktop_file)
                    injected = True
                return original_unlink(name, dir_fd=dir_fd)

            with mock.patch(
                "termdeck.desktop_integration._atomic_write_bytes",
                side_effect=fail_icon,
            ), mock.patch(
                "termdeck.desktop_integration.os.unlink",
                side_effect=replace_public_before_quarantine_unlink,
            ), self.assertRaisesRegex(OSError, "later publication failure"):
                install_desktop(plan)

            self.assertTrue(injected)
            self.assertEqual(plan.paths.desktop_file.read_bytes(), concurrent_content)
            self.assertFalse(plan.paths.icon_file.exists())

    def test_concurrent_installs_serialize_per_target_and_preserve_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_a = build_install_plan(command="/opt/a/t1000", data_home=tmp)
            plan_b = build_install_plan(command="/opt/b/t1000", data_home=tmp)
            original_write = desktop_integration._atomic_write_bytes
            first_published = threading.Event()
            second_started = threading.Event()
            allow_first_to_fail = threading.Event()
            errors: dict[str, BaseException] = {}
            results: dict[str, dict[str, object]] = {}

            def interleave(path, content, **kwargs):
                thread_name = threading.current_thread().name
                if thread_name == "desktop-installer-a" and Path(path) == plan_a.paths.desktop_file:
                    result = original_write(path, content, **kwargs)
                    first_published.set()
                    if not allow_first_to_fail.wait(5):
                        raise RuntimeError("concurrent-install test timed out")
                    return result
                if thread_name == "desktop-installer-a" and Path(path) == plan_a.paths.icon_file:
                    raise OSError("simulated installer A failure")
                return original_write(path, content, **kwargs)

            def run(name: str, plan) -> None:
                if name == "b":
                    second_started.set()
                try:
                    results[name] = install_desktop(plan)
                except BaseException as exc:  # captured for deterministic assertions
                    errors[name] = exc

            with mock.patch(
                "termdeck.desktop_integration._atomic_write_bytes",
                side_effect=interleave,
            ):
                first = threading.Thread(
                    target=run,
                    args=("a", plan_a),
                    name="desktop-installer-a",
                )
                second = threading.Thread(
                    target=run,
                    args=("b", plan_b),
                    name="desktop-installer-b",
                )
                first.start()
                self.assertTrue(first_published.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                allow_first_to_fail.set()
                first.join(5)
                second.join(5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertIsInstance(errors.get("a"), OSError)
            self.assertNotIn("b", errors)
            self.assertTrue(results["b"]["ok"])
            self.assertIn(
                "Exec=/opt/b/t1000 gui",
                plan_b.paths.desktop_file.read_text(encoding="utf-8"),
            )
            self.assertTrue(plan_b.paths.icon_file.exists())

    def test_uninstall_rejects_symlinked_xdg_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside_applications = outside / "applications"
            outside_applications.mkdir(parents=True)
            outside_desktop = outside_applications / DESKTOP_FILENAME
            outside_desktop.write_text("keep", encoding="utf-8")

            data_home = root / "data"
            data_home.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(DesktopIntegrationError, "safely"):
                uninstall_desktop(data_home=data_home)
            self.assertEqual(outside_desktop.read_text(encoding="utf-8"), "keep")

            config_home = root / "config"
            outside_autostart = root / "outside-autostart"
            outside_autostart.mkdir()
            outside_autostart_file = outside_autostart / AUTOSTART_FILENAME
            outside_autostart_file.write_text("keep", encoding="utf-8")
            config_home.mkdir()
            (config_home / "autostart").symlink_to(
                outside_autostart, target_is_directory=True
            )
            with self.assertRaisesRegex(DesktopIntegrationError, "safely"):
                uninstall_desktop(
                    data_home=root / "safe-data",
                    config_home=config_home,
                    remove_autostart=True,
                )
            self.assertEqual(outside_autostart_file.read_text(encoding="utf-8"), "keep")

    def test_uninstall_keeps_open_parent_when_path_is_swapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_home = root / "data"
            applications = data_home / "applications"
            applications.mkdir(parents=True)
            target = applications / DESKTOP_FILENAME
            target.write_text("managed", encoding="utf-8")

            outside = root / "outside"
            outside.mkdir()
            outside_target = outside / DESKTOP_FILENAME
            outside_target.write_text("outside", encoding="utf-8")
            moved_applications = root / "applications-before-race"
            original_rename = desktop_integration._rename_noreplace_at
            swapped = False

            def swap_parent_then_rename(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
            ):
                nonlocal swapped
                if not swapped and source_name == target.name:
                    applications.rename(moved_applications)
                    applications.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with mock.patch(
                "termdeck.desktop_integration._rename_noreplace_at",
                side_effect=swap_parent_then_rename,
            ):
                payload = uninstall_desktop(data_home=data_home)

            self.assertTrue(swapped)
            self.assertIn(str(target), payload["removed"])
            self.assertFalse(moved_applications.joinpath(DESKTOP_FILENAME).exists())
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "outside")
            self.assertTrue(applications.is_symlink())

    def test_uninstall_quarantine_unlink_preserves_public_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_install_plan(command="t1000", data_home=tmp)
            install_desktop(plan)
            original_unlink = desktop_integration.os.unlink
            concurrent_content = b"concurrent replacement during uninstall\n"
            injected = False

            def replace_public_before_quarantine_unlink(name, *, dir_fd=None):
                nonlocal injected
                if ".uninstall-" in name and not injected:
                    replacement = plan.paths.applications_dir / ".uninstall-concurrent"
                    replacement.write_bytes(concurrent_content)
                    os.replace(replacement, plan.paths.desktop_file)
                    injected = True
                return original_unlink(name, dir_fd=dir_fd)

            with mock.patch(
                "termdeck.desktop_integration.os.unlink",
                side_effect=replace_public_before_quarantine_unlink,
            ):
                payload = uninstall_desktop(data_home=tmp)

            self.assertTrue(injected)
            self.assertIn(str(plan.paths.desktop_file), payload["removed"])
            self.assertEqual(plan.paths.desktop_file.read_bytes(), concurrent_content)

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

            def fail_icon(path, content, **kwargs):
                nonlocal failed
                if Path(path) == plan.paths.icon_file and not failed:
                    failed = True
                    raise OSError("simulated icon publication failure")
                return original_write(path, content, **kwargs)

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
