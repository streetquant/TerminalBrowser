import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import termdeck.shutdown_service as shutdown_service
from termdeck.shutdown_service import (
    CANONICAL_MOUNT_ROOTS,
    CHECKPOINT_START_WRAPPER,
    CHECKPOINT_STOP_WRAPPER,
    ShutdownServiceError,
    SYSTEM_UNIT_MODE,
    SYSTEM_UNIT_NAME,
    TERMDECK_STATE_ROOT,
    USER_UNIT_NAME,
    install_system_checkpoint_service,
    install_user_checkpoint_service,
    render_system_checkpoint_service,
    render_user_checkpoint_service,
    verify_system_checkpoint_service,
    verify_user_checkpoint_service,
    uninstall_system_checkpoint_service,
    uninstall_user_checkpoint_service,
)


class ShutdownServiceTests(unittest.TestCase):
    @staticmethod
    def _write_executable(root: Path) -> Path:
        executable = root / "t1000"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    @staticmethod
    def _temporary_system_owner() -> tuple[int, int]:
        return os.getuid(), os.getgid()

    def test_rendered_legacy_user_unit_uses_managed_contract(self):
        # The text remains recognizable for legacy units, but installation is
        # rejected below because its managed stop wrapper requires root.
        unit = render_user_checkpoint_service("/home/test/.local/bin/t1000")

        self.assertIn("Before=exit.target", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn(
            "RequiresMountsFor="
            + " ".join(str(path) for path in CANONICAL_MOUNT_ROOTS),
            unit,
        )
        self.assertNotIn("ExecStop=-", unit)
        self.assertIn(f"ExecStart={CHECKPOINT_START_WRAPPER}", unit)
        self.assertIn(f"ExecStop={CHECKPOINT_STOP_WRAPPER}", unit)
        self.assertIn(f"Environment=HOME={TERMDECK_STATE_ROOT}", unit)
        self.assertNotIn("runuser", unit)
        self.assertNotIn("pkill", unit)
        self.assertIn("TimeoutStopSec=15min", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_system_unit_stops_before_user_manager_and_mounts(self):
        unit = render_system_checkpoint_service(
            "/home/arch/.local/bin/t1000",
            user="max",
            uid=1000,
            home="/home/arch",
            required_mounts=CANONICAL_MOUNT_ROOTS,
        )

        self.assertIn("After=warp.mount user@1000.service local-fs.target", unit)
        self.assertIn("Before=umount.target shutdown.target", unit)
        self.assertIn("ConditionPathIsMountPoint=/warp", unit)
        self.assertIn(f"ConditionPathIsDirectory={TERMDECK_STATE_ROOT}", unit)
        self.assertIn(
            f"ConditionPathIsDirectory={CHECKPOINT_START_WRAPPER.parent}",
            unit,
        )
        self.assertIn(
            "RequiresMountsFor="
            + " ".join(str(path) for path in CANONICAL_MOUNT_ROOTS),
            unit,
        )
        self.assertNotIn("ExecStop=-", unit)
        self.assertNotIn("User=", unit)
        self.assertNotIn("Group=", unit)
        self.assertIn(f"Environment=HOME={TERMDECK_STATE_ROOT}", unit)
        self.assertNotIn("Environment=USER=", unit)
        self.assertNotIn("Environment=LOGNAME=", unit)
        self.assertIn(f"ExecStart={CHECKPOINT_START_WRAPPER}", unit)
        self.assertIn(f"ExecStop={CHECKPOINT_STOP_WRAPPER}", unit)
        self.assertNotIn("runuser", unit)
        self.assertNotIn("pkill", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartMode=direct", unit)
        self.assertIn("TimeoutStopSec=15min", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_system_renderer_exactly_matches_checked_in_root_shutdown_hook(self):
        rendered = render_system_checkpoint_service(
            None,
            user="max",
            uid=1000,
            home="/home/arch",
            required_mounts=CANONICAL_MOUNT_ROOTS,
        )
        static_unit = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "systemd"
            / SYSTEM_UNIT_NAME
        ).read_text(encoding="utf-8")

        self.assertEqual(rendered, static_unit)
        self.assertTrue(
            verify_system_checkpoint_service(
                static_unit,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
        )

    def test_rendered_units_pass_exact_contract_verification(self):
        user_unit = render_user_checkpoint_service("/tmp/ignored-t1000")
        system_unit = render_system_checkpoint_service(
            "/tmp/ignored-t1000",
            user="max",
            uid=1000,
            home="/home/arch",
            required_mounts=CANONICAL_MOUNT_ROOTS,
        )

        self.assertTrue(verify_user_checkpoint_service(user_unit))
        self.assertTrue(
            verify_system_checkpoint_service(
                system_unit,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
        )

    def test_unit_verification_rejects_mount_and_stop_command_drift(self):
        unit = render_system_checkpoint_service(
            None,
            user="max",
            uid=1000,
            home="/home/arch",
            required_mounts=CANONICAL_MOUNT_ROOTS,
        )

        for drift in (
            unit.replace(
                "RequiresMountsFor=/warp/projects/softwares/termdeck /warp/projects/softwares/termdeck/.termdeck /warp/projects/softwares/termdeck/.termdeck/state",
                "RequiresMountsFor=/warp/projects/softwares/termdeck /warp/projects/softwares/termdeck/.termdeck /drift",
            ),
            unit.replace(
                f"ExecStop={CHECKPOINT_STOP_WRAPPER}",
                f"ExecStop=-{CHECKPOINT_STOP_WRAPPER}",
            ),
            unit.replace(
                f"Environment=HOME={TERMDECK_STATE_ROOT}",
                f"User=max\nEnvironment=HOME={TERMDECK_STATE_ROOT}",
            ),
        ):
            with self.assertRaises(ShutdownServiceError):
                verify_system_checkpoint_service(
                    drift,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                )

    def test_render_system_rejects_noncanonical_mount_drift(self):
        with self.assertRaisesRegex(ShutdownServiceError, "exactly"):
            render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=(CANONICAL_MOUNT_ROOTS[0], "/drift"),
            )

    def test_system_renderer_refuses_identity_that_differs_from_runuser_wrapper(self):
        for user, uid, home in (
            ("alice", 1000, "/home/arch"),
            ("max", 2000, "/home/arch"),
            ("max", 1000, "/home/alice"),
        ):
            with self.subTest(user=user, uid=uid, home=home), self.assertRaisesRegex(
                ShutdownServiceError,
                "identity must be exactly user='max', uid=1000, home='/home/arch'",
            ):
                render_system_checkpoint_service(
                    None,
                    user=user,
                    uid=uid,
                    home=home,
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                )

    def test_system_install_refuses_noncanonical_identity_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "identity must be exactly"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="alice",
                        uid=2000,
                        home="/home/alice",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=True,
                    )

            self.assertFalse(unit_dir.exists())
            systemctl.assert_not_called()

    def test_install_system_unit_requires_root_and_enables_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                result = install_system_checkpoint_service(
                    executable=executable,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                    unit_dir=unit_dir,
                    enable=True,
                )

            path = unit_dir / SYSTEM_UNIT_NAME
            content = path.read_text(encoding="utf-8")
            path_status = path.stat()

        self.assertEqual(result.path, path)
        self.assertTrue(result.enabled)
        self.assertIn("After=warp.mount user@1000.service", content)
        self.assertIn(f"ExecStart={CHECKPOINT_START_WRAPPER}", content)
        self.assertIn(f"ExecStop={CHECKPOINT_STOP_WRAPPER}", content)
        self.assertNotIn("ExecStop=-", content)
        self.assertEqual(stat.S_IMODE(path_status.st_mode), SYSTEM_UNIT_MODE)
        self.assertEqual((path_status.st_uid, path_status.st_gid), owner)
        self.assertEqual(
            systemctl.call_args_list,
            [
                mock.call(["daemon-reload"]),
                mock.call(["enable", "--now", SYSTEM_UNIT_NAME]),
            ],
        )

    def test_system_install_refuses_global_enable_for_a_custom_unit_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "custom-systemd"
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "systemctl enable requires the canonical system unit directory",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=True,
                    )

            self.assertFalse(unit_dir.exists())
            systemctl.assert_not_called()

    def test_system_install_rechecks_the_canonical_unit_after_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            owner = self._temporary_system_owner()
            path = unit_dir / SYSTEM_UNIT_NAME
            foreign = "[Unit]\nDescription=foreign systemctl replacement\n"
            foreign_status: os.stat_result | None = None

            def replace_after_reload(args: list[str]) -> None:
                nonlocal foreign_status
                if args != ["daemon-reload"]:
                    return
                replacement = unit_dir / "foreign-after-reload"
                replacement.write_text(foreign, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system",
                side_effect=replace_after_reload,
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "installed system checkpoint unit identity changed",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=True,
                    )

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(systemctl.call_args_list, [mock.call(["daemon-reload"])])

    def test_system_install_replaces_existing_unit_and_retains_its_private_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text("[Unit]\nDescription=old managed unit\n", encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                result = install_system_checkpoint_service(
                    executable=executable,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                    unit_dir=unit_dir,
                    enable=False,
                )

            self.assertEqual(result.path, path)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                render_system_checkpoint_service(
                    None,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                ),
            )
            retained = list(
                unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
            )
            self.assertEqual(len(retained), 1)
            self.assertEqual(
                sorted(child.name for child in unit_dir.iterdir() if not child.name.startswith(".")),
                [SYSTEM_UNIT_NAME],
            )
            self.assertEqual(
                (retained[0] / "payload").read_text(encoding="utf-8"),
                "[Unit]\nDescription=old managed unit\n",
            )
            self.assertIn("private cleanup evidence retained", result.detail)
            systemctl.assert_not_called()

    def test_system_install_receipt_marks_private_stage_swap_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()
            foreign_stage = root / "foreign-private-stage"
            foreign_stage.mkdir()
            foreign_status = foreign_stage.stat()

            def swap_private_stage(phase: str) -> None:
                if phase != "before-stage-directory-retain":
                    return
                stage_path = next(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.install-*"))
                os.rename(stage_path, unit_dir / ".detached-private-stage")
                os.rename(foreign_stage, stage_path)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_cleanup_probe",
                side_effect=swap_private_stage,
            ), mock.patch(
                "termdeck.shutdown_service.os.unlink",
                side_effect=AssertionError("private cleanup must not unlink"),
            ) as unlink, mock.patch(
                "termdeck.shutdown_service.os.rmdir",
                side_effect=AssertionError("private cleanup must not rmdir"),
            ) as rmdir, mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                result = install_system_checkpoint_service(
                    executable=executable,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                    unit_dir=unit_dir,
                    enable=False,
                )

            path = unit_dir / SYSTEM_UNIT_NAME
            retained = list(
                unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
            )
            self.assertEqual(path.read_text(encoding="utf-8"), render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            ))
            self.assertEqual(
                sorted(child.name for child in unit_dir.iterdir() if not child.name.startswith(".")),
                [SYSTEM_UNIT_NAME],
            )
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].stat().st_ino, foreign_status.st_ino)
            self.assertIn("cleanup status is ambiguous", result.detail)
            unlink.assert_not_called()
            rmdir.assert_not_called()
            systemctl.assert_not_called()

    def test_system_install_rejects_stage_directory_swapped_before_first_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()
            foreign_stage = root / "foreign-stage-before-first-stat"
            foreign_stage.mkdir()
            foreign_stage.chmod(0o711)
            sentinel = foreign_stage / "foreign-content"
            sentinel.write_text("do not modify this foreign stage\n", encoding="utf-8")
            foreign_status = foreign_stage.stat()
            detached_stage: Path | None = None

            def swap_before_first_stat(phase: str) -> None:
                nonlocal detached_stage
                if phase != "stage-after-mkdir-before-first-stat":
                    return
                stage_path = next(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.install-*"))
                detached_stage = unit_dir / ".detached-owned-stage-before-first-stat"
                os.rename(stage_path, detached_stage)
                os.rename(foreign_stage, stage_path)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_creation_probe",
                side_effect=swap_before_first_stat,
            ), mock.patch("termdeck.shutdown_service.os.fchown") as fchown, mock.patch(
                "termdeck.shutdown_service.os.fchmod"
            ) as fchmod, mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "creation fence observed a name transition",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertIsNotNone(detached_stage)
            foreign_at_stage = next(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.install-*"))
            self.assertEqual(foreign_at_stage.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(stat.S_IMODE(foreign_at_stage.stat().st_mode), 0o711)
            self.assertEqual(
                (foreign_at_stage / "foreign-content").read_text(encoding="utf-8"),
                "do not modify this foreign stage\n",
            )
            fchown.assert_not_called()
            fchmod.assert_not_called()
            systemctl.assert_not_called()

    def test_system_install_rejects_stage_directory_swapped_before_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()
            foreign_stage = root / "foreign-stage"
            foreign_stage.mkdir()
            foreign_stage.chmod(0o711)
            foreign_status = foreign_stage.stat()
            detached_stage: Path | None = None

            def swap_before_open(phase: str) -> None:
                nonlocal detached_stage
                if phase != "after-stage-mkdir-preimage":
                    return
                stage_path = next(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.install-*"))
                detached_stage = unit_dir / ".detached-owned-stage"
                os.rename(stage_path, detached_stage)
                os.rename(foreign_stage, stage_path)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_stage_probe",
                side_effect=swap_before_open,
            ), mock.patch("termdeck.shutdown_service.os.fchmod") as fchmod, mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "creation fence observed a name transition",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertIsNotNone(detached_stage)
            foreign_at_stage = next(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.install-*"))
            self.assertEqual(foreign_at_stage.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(stat.S_IMODE(foreign_at_stage.stat().st_mode), 0o711)
            self.assertFalse((foreign_at_stage / "payload").exists())
            fchmod.assert_not_called()
            systemctl.assert_not_called()

    def test_system_unit_parent_creation_rejects_swap_before_first_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "created-parent" / "systemd"
            owner = self._temporary_system_owner()
            foreign_parent = root / "foreign-created-parent"
            foreign_parent.mkdir()
            foreign_parent.chmod(0o711)
            sentinel = foreign_parent / "foreign-content"
            sentinel.write_text("do not modify this foreign parent\n", encoding="utf-8")
            foreign_status = foreign_parent.stat()
            detached_parent: Path | None = None

            def swap_before_first_stat(phase: str) -> None:
                nonlocal detached_parent
                if (
                    phase
                    != "directory-component-after-mkdir-before-first-stat:created-parent"
                ):
                    return
                created_parent = root / "created-parent"
                detached_parent = root / ".detached-owned-created-parent"
                os.rename(created_parent, detached_parent)
                os.rename(foreign_parent, created_parent)

            with mock.patch(
                "termdeck.shutdown_service._system_unit_creation_probe",
                side_effect=swap_before_first_stat,
            ), mock.patch("termdeck.shutdown_service.os.fchown") as fchown, mock.patch(
                "termdeck.shutdown_service.os.fchmod"
            ) as fchmod:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "creation fence observed a name transition",
                ):
                    shutdown_service._open_system_unit_directory(
                        unit_dir,
                        label="system unit directory",
                        create_parents=True,
                        owner=owner,
                    )

            self.assertIsNotNone(detached_parent)
            foreign_at_parent = root / "created-parent"
            self.assertEqual(foreign_at_parent.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(stat.S_IMODE(foreign_at_parent.stat().st_mode), 0o711)
            self.assertEqual(
                (foreign_at_parent / "foreign-content").read_text(encoding="utf-8"),
                "do not modify this foreign parent\n",
            )
            fchown.assert_not_called()
            fchmod.assert_not_called()

    def test_system_install_rejects_eexist_after_missing_parent_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "raced-systemd"
            owner = self._temporary_system_owner()
            foreign_dir = root / "foreign-raced-systemd"
            foreign_dir.mkdir()
            foreign_dir.chmod(0o711)
            sentinel = foreign_dir / "foreign-content"
            sentinel.write_text("do not modify this foreign directory\n", encoding="utf-8")
            foreign_status = foreign_dir.stat()
            real_mkdir = shutdown_service.os.mkdir
            raced = False

            def create_foreign_then_report_eexist(
                name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> None:
                nonlocal raced
                if name == "raced-systemd" and dir_fd is not None and not raced:
                    raced = True
                    os.rename(foreign_dir, unit_dir)
                    raise FileExistsError("injected raced directory")
                real_mkdir(name, mode, dir_fd=dir_fd)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service.os.mkdir",
                side_effect=create_foreign_then_report_eexist,
            ), mock.patch("termdeck.shutdown_service.os.fchown") as fchown, mock.patch(
                "termdeck.shutdown_service.os.fchmod"
            ) as fchmod, mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "component appeared after it was observed absent",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertTrue(raced)
            self.assertEqual(unit_dir.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(stat.S_IMODE(unit_dir.stat().st_mode), 0o711)
            self.assertEqual(
                (unit_dir / "foreign-content").read_text(encoding="utf-8"),
                "do not modify this foreign directory\n",
            )
            self.assertFalse((unit_dir / SYSTEM_UNIT_NAME).exists())
            fchown.assert_not_called()
            fchmod.assert_not_called()
            systemctl.assert_not_called()

    def test_system_install_stage_open_failure_has_a_retained_error_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()
            real_open = shutdown_service.os.open

            def deny_stage_open(
                name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if (
                    isinstance(name, str)
                    and name.startswith(f".{SYSTEM_UNIT_NAME}.install-")
                    and dir_fd is not None
                ):
                    raise PermissionError("injected stage directory open denial")
                return real_open(name, flags, mode, dir_fd=dir_fd)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service.os.open", side_effect=deny_stage_open), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system"
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "could not open created system unit stage directory.*private cleanup evidence retained",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertFalse((unit_dir / SYSTEM_UNIT_NAME).exists())
            self.assertEqual(
                len(list(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*"))),
                1,
            )
            systemctl.assert_not_called()

    def test_system_install_fails_if_public_unit_reappears_during_stage_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign retention race\n"
            foreign_status: os.stat_result | None = None

            def reoccupy_public_unit(phase: str) -> None:
                nonlocal foreign_status
                if phase != "before-stage-directory-retain":
                    return
                replacement = unit_dir / "foreign-retention-race"
                replacement.write_text(foreign, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_cleanup_probe",
                side_effect=reoccupy_public_unit,
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "published system unit identity changed",
                ):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_system_install_refuses_parent_swap_without_touching_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign parent replacement\n"
            foreign_path = unit_dir / SYSTEM_UNIT_NAME

            def swap_parent(phase: str) -> None:
                if phase != "after-preimage":
                    return
                os.rename(unit_dir, root / "detached-systemd")
                unit_dir.mkdir()
                foreign_path.write_text(foreign, encoding="utf-8")

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_install_probe", side_effect=swap_parent
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "parent identity changed"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertEqual(foreign_path.read_text(encoding="utf-8"), foreign)
            systemctl.assert_not_called()

    def test_system_install_refuses_linked_parent_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            foreign_dir = root / "foreign-systemd"
            foreign_dir.mkdir()
            unit_dir = root / "systemd"
            unit_dir.symlink_to(foreign_dir, target_is_directory=True)
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "is not a directory"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertFalse((foreign_dir / SYSTEM_UNIT_NAME).exists())
            systemctl.assert_not_called()

    def test_system_install_rejects_existing_fifo_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            os.mkfifo(path)
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "not a regular file"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertTrue(stat.S_ISFIFO(os.lstat(path).st_mode))
            systemctl.assert_not_called()

    def test_system_install_restores_late_leaf_swap_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text("[Unit]\nDescription=managed old unit\n", encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign late leaf\n"
            foreign_status: os.stat_result | None = None

            def swap_leaf(phase: str) -> None:
                nonlocal foreign_status
                if phase != "after-target-cas":
                    return
                replacement = unit_dir / "foreign-leaf"
                replacement.write_text(foreign, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_install_probe", side_effect=swap_leaf
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "target changed during atomic publish"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_system_install_uses_no_replace_for_a_leaf_created_after_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign create-only leaf\n"
            foreign_status: os.stat_result | None = None

            def create_leaf(phase: str) -> None:
                nonlocal foreign_status
                if phase != "after-target-cas":
                    return
                path.write_text(foreign, encoding="utf-8")
                path.chmod(SYSTEM_UNIT_MODE)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_install_probe", side_effect=create_leaf
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "without replacement"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_system_install_restores_a_late_symlink_leaf_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text("[Unit]\nDescription=managed old unit\n", encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            foreign_target = root / "foreign-unit"
            foreign_target.write_text("foreign target\n", encoding="utf-8")
            owner = self._temporary_system_owner()

            def swap_leaf_for_link(phase: str) -> None:
                if phase != "after-target-cas":
                    return
                path.unlink()
                path.symlink_to(foreign_target)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_install_probe",
                side_effect=swap_leaf_for_link,
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "target changed during atomic publish"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), str(foreign_target))
            self.assertEqual(foreign_target.read_text(encoding="utf-8"), "foreign target\n")
            systemctl.assert_not_called()

    def test_system_install_rolls_back_after_publish_directory_fsync_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = self._write_executable(root)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            original = "[Unit]\nDescription=old durable unit\n"
            path.write_text(original, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            original_status = path.stat()
            owner = self._temporary_system_owner()
            original_fsync_parent = shutdown_service._fsync_system_unit_parent
            failed = False

            def fail_publish_fsync(parent, *, phase: str) -> None:
                nonlocal failed
                if phase == "publish" and not failed:
                    failed = True
                    raise ShutdownServiceError("injected publish directory fsync failure")
                original_fsync_parent(parent, phase=phase)

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._fsync_system_unit_parent", side_effect=fail_publish_fsync
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "injected publish directory fsync failure"):
                    install_system_checkpoint_service(
                        executable=executable,
                        user="max",
                        uid=1000,
                        home="/home/arch",
                        required_mounts=CANONICAL_MOUNT_ROOTS,
                        unit_dir=unit_dir,
                        enable=False,
                    )

            self.assertTrue(failed)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            restored_status = path.stat()
            self.assertEqual(restored_status.st_ino, original_status.st_ino)
            self.assertEqual(stat.S_IMODE(restored_status.st_mode), stat.S_IMODE(original_status.st_mode))
            self.assertEqual(
                (restored_status.st_uid, restored_status.st_gid),
                (original_status.st_uid, original_status.st_gid),
            )
            retained = list(
                unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
            )
            self.assertEqual(len(retained), 1)
            self.assertEqual(
                sorted(child.name for child in unit_dir.iterdir() if not child.name.startswith(".")),
                [SYSTEM_UNIT_NAME],
            )
            systemctl.assert_not_called()

    def test_user_install_fails_closed_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"

            with mock.patch(
                "termdeck.shutdown_service._resolve_t1000_executable"
            ) as resolve, mock.patch(
                "termdeck.shutdown_service.os.replace"
            ) as replace, mock.patch(
                "termdeck.shutdown_service._run_systemctl"
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "requires root to invoke /usr/bin/runuser",
                ):
                    install_user_checkpoint_service(
                        unit_dir=unit_dir,
                        enable=True,
                    )

            self.assertFalse(unit_dir.exists())
            resolve.assert_not_called()
            replace.assert_not_called()
            systemctl.assert_not_called()

    def test_uninstall_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / USER_UNIT_NAME
            path.write_text(render_user_checkpoint_service(), encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                result = uninstall_user_checkpoint_service(
                    unit_dir=root,
                    disable=True,
                )

            self.assertFalse(path.exists())
            retained = list(root.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(
                (retained[0] / "removed-checkpoint-unit").read_text(encoding="utf-8"),
                render_user_checkpoint_service(),
            )
            self.assertFalse(result.enabled)
            self.assertIn("private cleanup evidence retained", result.detail)
            self.assertEqual(
                systemctl.call_args_list,
                [
                    mock.call(["disable", "--now", USER_UNIT_NAME]),
                    mock.call(["daemon-reload"]),
                ],
            )

    def test_system_uninstall_removes_only_the_anchored_regular_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text(
                render_system_checkpoint_service(
                    None,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                ),
                encoding="utf-8",
            )
            path.chmod(SYSTEM_UNIT_MODE)
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                result = uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertEqual(result.path, path)
            self.assertFalse(path.exists())
            retained = list(
                unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
            )
            self.assertEqual(len(retained), 1)
            self.assertEqual(
                (retained[0] / "removed-checkpoint-unit").read_text(encoding="utf-8"),
                render_system_checkpoint_service(
                    None,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                ),
            )
            self.assertIn("private cleanup evidence retained", result.detail)
            self.assertEqual(
                systemctl.call_args_list,
                [
                    mock.call(["disable", "--now", SYSTEM_UNIT_NAME]),
                    mock.call(["daemon-reload"]),
                ],
            )

    def test_system_uninstall_refuses_global_disable_for_a_custom_unit_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "custom-systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            managed = render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
            path.write_text(managed, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            original_status = path.stat()
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "systemctl disable requires the canonical system unit directory",
                ):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertEqual(path.read_text(encoding="utf-8"), managed)
            self.assertEqual(path.stat().st_ino, original_status.st_ino)
            systemctl.assert_not_called()

    def test_system_uninstall_requires_exact_managed_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            managed = render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
            path.write_text(managed, encoding="utf-8")
            path.chmod(0o600)
            original_status = path.stat()
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "mode does not match the managed contract",
                ):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=False)

            self.assertEqual(path.read_text(encoding="utf-8"), managed)
            self.assertEqual(path.stat().st_ino, original_status.st_ino)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            systemctl.assert_not_called()

    def test_absent_system_uninstall_reports_a_suppressed_global_disable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            owner = self._temporary_system_owner()

            def fail_disable(args: list[str]) -> None:
                if args == ["disable", "--now", SYSTEM_UNIT_NAME]:
                    raise ShutdownServiceError("injected global disable failure")

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system",
                side_effect=fail_disable,
            ) as systemctl:
                result = uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertFalse((unit_dir / SYSTEM_UNIT_NAME).exists())
            self.assertIn("was already absent", result.detail)
            self.assertIn("systemctl disable was not completed", result.detail)
            self.assertEqual(
                systemctl.call_args_list,
                [mock.call(["disable", "--now", SYSTEM_UNIT_NAME])],
            )

    def test_system_uninstall_refuses_same_owner_nonmanaged_content_before_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            foreign = "[Unit]\nDescription=same-owner foreign unit\n"
            path.write_text(foreign, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            foreign_status = path.stat()
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "does not match the managed contract"):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=False)

            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_user_uninstall_refuses_same_owner_nonmanaged_content_before_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / USER_UNIT_NAME
            foreign = "[Unit]\nDescription=same-owner foreign user unit\n"
            path.write_text(foreign, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "does not match the managed contract"):
                    uninstall_user_checkpoint_service(unit_dir=root, disable=True)

            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_user_uninstall_requires_exact_managed_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / USER_UNIT_NAME
            managed = render_user_checkpoint_service()
            path.write_text(managed, encoding="utf-8")
            path.chmod(0o600)
            original_status = path.stat()

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "mode does not match the managed contract",
                ):
                    uninstall_user_checkpoint_service(unit_dir=root, disable=False)

            self.assertEqual(path.read_text(encoding="utf-8"), managed)
            self.assertEqual(path.stat().st_ino, original_status.st_ino)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            systemctl.assert_not_called()

    def test_user_uninstall_rejects_leaf_replaced_during_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / USER_UNIT_NAME
            managed = render_user_checkpoint_service()
            path.write_text(managed, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            initial_status = path.stat()

            def replace_during_disable(args: list[str]) -> None:
                if args != ["disable", "--now", USER_UNIT_NAME]:
                    return
                replacement = root / "same-content-user-replacement"
                replacement.write_text(managed, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)

            with mock.patch(
                "termdeck.shutdown_service._run_systemctl",
                side_effect=replace_during_disable,
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "identity changed after removal preflight",
                ):
                    uninstall_user_checkpoint_service(unit_dir=root, disable=True)

            self.assertEqual(path.read_text(encoding="utf-8"), managed)
            self.assertNotEqual(path.stat().st_ino, initial_status.st_ino)
            self.assertEqual(
                systemctl.call_args_list,
                [mock.call(["disable", "--now", USER_UNIT_NAME])],
            )

    def test_system_uninstall_rejects_same_content_leaf_replaced_during_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            managed = render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
            path.write_text(managed, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            initial_status = path.stat()
            owner = self._temporary_system_owner()

            def replace_during_disable(args: list[str]) -> None:
                if args != ["disable", "--now", SYSTEM_UNIT_NAME]:
                    return
                replacement = unit_dir / "same-content-replacement"
                replacement.write_text(managed, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system",
                side_effect=replace_during_disable,
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "identity changed after removal preflight",
                ):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertEqual(path.read_text(encoding="utf-8"), managed)
            self.assertNotEqual(path.stat().st_ino, initial_status.st_ino)
            self.assertEqual(
                systemctl.call_args_list,
                [mock.call(["disable", "--now", SYSTEM_UNIT_NAME])],
            )

    def test_system_uninstall_rejects_content_changed_during_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            managed = render_system_checkpoint_service(
                None,
                user="max",
                uid=1000,
                home="/home/arch",
                required_mounts=CANONICAL_MOUNT_ROOTS,
            )
            path.write_text(managed, encoding="utf-8")
            path.chmod(SYSTEM_UNIT_MODE)
            foreign = "[Unit]\nDescription=changed during systemctl\n"
            owner = self._temporary_system_owner()

            def mutate_during_disable(args: list[str]) -> None:
                if args == ["disable", "--now", SYSTEM_UNIT_NAME]:
                    path.write_text(foreign, encoding="utf-8")
                    path.chmod(SYSTEM_UNIT_MODE)

            with mock.patch("termdeck.shutdown_service.DEFAULT_SYSTEM_UNIT_DIR", unit_dir), mock.patch(
                "termdeck.shutdown_service.os.geteuid", return_value=0
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._run_systemctl_system",
                side_effect=mutate_during_disable,
            ) as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "identity changed after removal preflight",
                ):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(
                systemctl.call_args_list,
                [mock.call(["disable", "--now", SYSTEM_UNIT_NAME])],
            )

    def test_user_uninstall_rejects_linked_unit_directory_without_touching_foreign_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign_dir = root / "foreign-user-systemd"
            foreign_dir.mkdir()
            foreign_path = foreign_dir / USER_UNIT_NAME
            foreign = "[Unit]\nDescription=foreign user unit\n"
            foreign_path.write_text(foreign, encoding="utf-8")
            unit_dir = root / "user-systemd"
            unit_dir.symlink_to(foreign_dir, target_is_directory=True)

            with mock.patch("termdeck.shutdown_service._run_systemctl") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "is not a directory"):
                    uninstall_user_checkpoint_service(unit_dir=unit_dir, disable=True)

            self.assertEqual(foreign_path.read_text(encoding="utf-8"), foreign)
            systemctl.assert_not_called()

    def test_system_uninstall_rejects_linked_unit_directory_without_touching_foreign_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign_dir = root / "foreign-systemd"
            foreign_dir.mkdir()
            foreign_path = foreign_dir / SYSTEM_UNIT_NAME
            foreign = "[Unit]\nDescription=foreign system unit\n"
            foreign_path.write_text(foreign, encoding="utf-8")
            unit_dir = root / "systemd"
            unit_dir.symlink_to(foreign_dir, target_is_directory=True)
            owner = self._temporary_system_owner()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "is not a directory"):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=False)

            self.assertEqual(foreign_path.read_text(encoding="utf-8"), foreign)
            systemctl.assert_not_called()

    def test_system_uninstall_restores_a_leaf_swapped_before_removal_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text(
                render_system_checkpoint_service(
                    None,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                ),
                encoding="utf-8",
            )
            path.chmod(SYSTEM_UNIT_MODE)
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign late leaf\n"
            foreign_status: os.stat_result | None = None

            def swap_leaf(phase: str) -> None:
                nonlocal foreign_status
                if phase != "before-removal-cas":
                    return
                replacement = unit_dir / "foreign-leaf"
                replacement.write_text(foreign, encoding="utf-8")
                replacement.chmod(SYSTEM_UNIT_MODE)
                os.replace(replacement, path)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_removal_probe", side_effect=swap_leaf
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(
                    ShutdownServiceError,
                    "identity changed after removal preflight",
                ):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=False)

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            self.assertEqual(
                len(list(unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*"))),
                1,
            )
            systemctl.assert_not_called()

    def test_system_uninstall_does_not_claim_success_when_a_foreign_leaf_reappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            path = unit_dir / SYSTEM_UNIT_NAME
            path.write_text(
                render_system_checkpoint_service(
                    None,
                    user="max",
                    uid=1000,
                    home="/home/arch",
                    required_mounts=CANONICAL_MOUNT_ROOTS,
                ),
                encoding="utf-8",
            )
            path.chmod(SYSTEM_UNIT_MODE)
            owner = self._temporary_system_owner()
            foreign = "[Unit]\nDescription=foreign reoccupying leaf\n"
            foreign_status: os.stat_result | None = None

            def reoccupy_leaf(phase: str) -> None:
                nonlocal foreign_status
                if phase != "after-removal-isolation":
                    return
                path.write_text(foreign, encoding="utf-8")
                path.chmod(SYSTEM_UNIT_MODE)
                foreign_status = path.stat()

            with mock.patch("termdeck.shutdown_service.os.geteuid", return_value=0), mock.patch(
                "termdeck.shutdown_service._system_unit_target_owner", return_value=owner
            ), mock.patch(
                "termdeck.shutdown_service._system_unit_removal_probe",
                side_effect=reoccupy_leaf,
            ), mock.patch("termdeck.shutdown_service._run_systemctl_system") as systemctl:
                with self.assertRaisesRegex(ShutdownServiceError, "target appeared after atomic removal"):
                    uninstall_system_checkpoint_service(unit_dir=unit_dir, disable=False)

            self.assertIsNotNone(foreign_status)
            self.assertEqual(path.read_text(encoding="utf-8"), foreign)
            self.assertEqual(path.stat().st_ino, foreign_status.st_ino)
            systemctl.assert_not_called()

    def test_stage_file_retention_preserves_foreign_leaf_swapped_before_retain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            owner = self._temporary_system_owner()
            parent = shutdown_service._open_system_unit_directory(
                unit_dir,
                label="system unit directory",
                create_parents=True,
                owner=owner,
            )
            stage = shutdown_service._create_system_stage_directory(parent, owner=owner)
            staged = shutdown_service._stage_system_unit_file(
                stage,
                name="payload",
                content=b"managed stage payload\n",
                owner=owner,
            )
            foreign = root / "foreign-stage-leaf"
            foreign.write_text("foreign stage leaf\n", encoding="utf-8")
            foreign_status = foreign.stat()
            stage_path = unit_dir / stage.name

            def swap_leaf(phase: str) -> None:
                if phase != "race-file-retention:before-stage-file-retain":
                    return
                os.replace(foreign, stage_path / "payload")

            try:
                with mock.patch(
                    "termdeck.shutdown_service._system_unit_cleanup_probe",
                    side_effect=swap_leaf,
                ), mock.patch(
                    "termdeck.shutdown_service.os.unlink",
                    side_effect=AssertionError("private cleanup must not unlink"),
                ) as unlink, mock.patch(
                    "termdeck.shutdown_service.os.rmdir",
                    side_effect=AssertionError("private cleanup must not rmdir"),
                ) as rmdir:
                    with self.assertRaisesRegex(
                        ShutdownServiceError,
                        "retained with ambiguous cleanup status",
                    ):
                        shutdown_service._retain_stage_file_if_exact(
                            stage,
                            "payload",
                            staged.identity,
                            phase="race-file-retention",
                        )
                    status = shutdown_service._retain_system_stage_best_effort(stage)

                self.assertEqual(status, "ambiguous")
                self.assertEqual(stage.cleanup_status, "ambiguous")
                retained_stages = list(
                    unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
                )
                self.assertEqual(len(retained_stages), 1)
                retained_leaf = next(
                    child
                    for child in retained_stages[0].iterdir()
                    if ".retained-file-" in child.name
                )
                self.assertEqual(retained_leaf.read_text(encoding="utf-8"), "foreign stage leaf\n")
                self.assertEqual(retained_leaf.stat().st_ino, foreign_status.st_ino)
                unlink.assert_not_called()
                rmdir.assert_not_called()
            finally:
                staged.close()
                stage.close()
                parent.close()

    def test_stage_directory_retention_preserves_foreign_directory_swapped_before_retain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_dir = root / "systemd"
            owner = self._temporary_system_owner()
            parent = shutdown_service._open_system_unit_directory(
                unit_dir,
                label="system unit directory",
                create_parents=True,
                owner=owner,
            )
            stage = shutdown_service._create_system_stage_directory(parent, owner=owner)
            foreign = root / "foreign-stage-directory"
            foreign.mkdir()
            foreign_status = foreign.stat()
            detached: Path | None = None

            def swap_directory(phase: str) -> None:
                nonlocal detached
                if phase != "before-stage-directory-retain":
                    return
                detached = unit_dir / "detached-owned-stage"
                os.rename(unit_dir / stage.name, detached)
                os.rename(foreign, unit_dir / stage.name)

            try:
                with mock.patch(
                    "termdeck.shutdown_service._system_unit_cleanup_probe",
                    side_effect=swap_directory,
                ), mock.patch(
                    "termdeck.shutdown_service.os.unlink",
                    side_effect=AssertionError("private cleanup must not unlink"),
                ) as unlink, mock.patch(
                    "termdeck.shutdown_service.os.rmdir",
                    side_effect=AssertionError("private cleanup must not rmdir"),
                ) as rmdir:
                    status = shutdown_service._retain_system_stage_best_effort(stage)

                self.assertEqual(status, "ambiguous")
                self.assertEqual(stage.cleanup_status, "ambiguous")
                retained_stages = list(
                    unit_dir.glob(f".{SYSTEM_UNIT_NAME}.retained-directory-*")
                )
                self.assertEqual(len(retained_stages), 1)
                self.assertTrue(retained_stages[0].is_dir())
                self.assertEqual(retained_stages[0].stat().st_ino, foreign_status.st_ino)
                unlink.assert_not_called()
                rmdir.assert_not_called()
            finally:
                stage.close()
                parent.close()


if __name__ == "__main__":
    unittest.main()
