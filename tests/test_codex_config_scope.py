import json
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck import codex_config_scope as config_scope
from termdeck.native_config_view import prepare_private_config, RECEIPT
from termdeck.codex_config_scope import (
    CodexConfigScopeError,
    migrate_quiescent_codex_configuration as _migrate_quiescent_codex_configuration,
    prepare_private_bash_launch_command,
    prepare_shared_codex_configuration as _prepare_shared_codex_configuration,
    resolve_codex_launcher,
)


_ATTESTED_TEST_LAUNCHERS: set[Path] = set()


def _attest_test_launcher(candidate: Path) -> bool:
    return candidate in _ATTESTED_TEST_LAUNCHERS


def prepare_shared_codex_configuration(private_home, canonical_home):
    return _prepare_shared_codex_configuration(
        private_home,
        canonical_home,
        launcher_attestor=_attest_test_launcher,
    )


def migrate_quiescent_codex_configuration(private_home):
    return _migrate_quiescent_codex_configuration(
        private_home,
        launcher_attestor=_attest_test_launcher,
    )


def _write_launcher(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["TERMDECK_TEST_CODEX_RECORD"]).write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "codex_home": os.environ.get("CODEX_HOME"),
            "codex_termdeck_gate_root": os.environ.get("CODEX_TERMDECK_GATE_ROOT"),
            "home": os.environ.get("HOME"),
        }
    ),
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    _ATTESTED_TEST_LAUNCHERS.add(path.resolve())


class CodexConfigScopeTests(unittest.TestCase):
    def test_receipt_backed_native_config_survives_shared_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, private, launcher = root / "canonical", root / "auth-test", root / "codex"
            source.mkdir()
            (source / "config.toml").write_text("model = 'fixture'\n")
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                prepare_shared_codex_configuration(private, source)
                prepare_private_config(private, source, source / "config.toml")
                with (private / "config.toml").open("a") as output:
                    output.write("[projects.\"/fixture\"]\ntrust_level = 'trusted'\n")
                view, receipt = (private / "config.toml").read_bytes(), (private / RECEIPT).read_bytes()
                status = prepare_shared_codex_configuration(private, source)
                plan = config_scope._configuration_plan(private, source)
            self.assertTrue(status.ready)
            self.assertFalse(status.migration_required)
            self.assertTrue(plan.private_native_config)
            self.assertEqual(plan.assets[0][3], "private-native")
            self.assertEqual((private / "config.toml").read_bytes(), view)
            self.assertEqual((private / RECEIPT).read_bytes(), receipt)
            self.assertEqual((source / "config.toml").read_text(), "model = 'fixture'\n")
            with self.assertRaisesRegex(CodexConfigScopeError, "requires explicit repair"):
                config_scope.validate_existing_private_bash_scope(private, auth_scope_id=private.name)

    def test_receipt_backed_native_config_without_source_file_is_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, private, launcher = root / "canonical", root / "auth-test", root / "codex"
            source.mkdir()
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                prepare_shared_codex_configuration(private, source)
                prepare_private_config(private, source, None)
                view = (private / "config.toml").read_bytes()
                status = prepare_shared_codex_configuration(private, source)
            self.assertTrue(status.ready)
            self.assertFalse(status.migration_required)
            self.assertEqual((private / "config.toml").read_bytes(), view)
            self.assertFalse((source / "config.toml").exists())
            with self.assertRaisesRegex(CodexConfigScopeError, "requires explicit repair"):
                config_scope.validate_existing_private_bash_scope(private, auth_scope_id=private.name)

    def test_invalid_native_receipt_refuses_scope_preparation_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, private, launcher = root / "canonical", root / "auth-test", root / "codex"
            source.mkdir()
            (source / "config.toml").write_text("model = 'fixture'\n")
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                prepare_shared_codex_configuration(private, source)
                prepare_private_config(private, source, source / "config.toml")
                (private / RECEIPT).write_text("{}\n")
                view = (private / "config.toml").read_bytes()
                with self.assertRaises(CodexConfigScopeError):
                    prepare_shared_codex_configuration(private, source)
            self.assertEqual((private / "config.toml").read_bytes(), view)
            self.assertEqual((private / RECEIPT).read_text(), "{}\n")

    def test_managed_bash_sources_user_rc_then_restores_private_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_home = root / "user"
            private = root / "auth-test"
            user_home.mkdir()
            private.mkdir()
            (user_home / ".bashrc").write_text(
                "export PATH=/user/rewrote/path:/usr/bin\n"
                "export TERMDECK_TEST_USER_RC=loaded\n",
                encoding="utf-8",
            )

            command = prepare_private_bash_launch_command(
                private,
                auth_scope_id="auth-test",
                shell_path="/bin/bash",
            )
            argv = shlex.split(command)
            completed = subprocess.run(
                [
                    *argv,
                    "-c",
                    "printf '%s\\n' \"$PATH\" \"$CODEX_HOME\" "
                    "\"$TERMDECK_AUTH_SCOPE_ID\" \"$CODEX_TERMDECK_GATE_ROOT\" "
                    "\"$TERMDECK_TEST_USER_RC\"",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(user_home)},
            )

            lines = completed.stdout.splitlines()
            self.assertEqual(
                lines,
                [
                    f"{private.resolve() / 'bin'}:/user/rewrote/path:/usr/bin",
                    str(private.resolve()),
                    "auth-test",
                    str(private.resolve() / ".termdeck-codex-lifetime"),
                    "loaded",
                ],
            )
            rc_path = private / "bin" / ".termdeck-bashrc"
            self.assertTrue(rc_path.is_file())
            self.assertEqual(stat.S_IMODE(rc_path.stat().st_mode), 0o700)

    def test_managed_bash_rejects_scope_mismatch_and_non_bash_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "auth-test"
            private.mkdir()

            with self.assertRaisesRegex(CodexConfigScopeError, "must match"):
                prepare_private_bash_launch_command(
                    private,
                    auth_scope_id="auth-other",
                    shell_path="/bin/bash",
                )
            with self.assertRaisesRegex(CodexConfigScopeError, "Bash only"):
                prepare_private_bash_launch_command(
                    private,
                    auth_scope_id="auth-test",
                    shell_path="/bin/true",
                )

    def test_new_scope_shares_canonical_assets_and_keeps_state_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            (source / "config.toml").write_text(
                'model = "gpt-5"\nsqlite_home = "/canonical/state"\n',
                encoding="utf-8",
            )
            (source / "ollama-launch.config.toml").write_text(
                'model_provider = "ollama"\n', encoding="utf-8"
            )
            (source / "AGENTS.md").write_text("canonical guidance\n", encoding="utf-8")
            (source / "agents").mkdir()
            (source / "skills").mkdir()
            private.mkdir()
            (private / "auth.json").write_text("private-auth\n", encoding="utf-8")
            (private / "current").write_text("work\n", encoding="utf-8")
            (private / "state_5.sqlite").write_bytes(b"private-state")
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                status = prepare_shared_codex_configuration(private, source)

            self.assertTrue(status.ready)
            self.assertFalse(status.migration_required)
            for name in (
                "config.toml",
                "ollama-launch.config.toml",
                "AGENTS.md",
                "agents",
                "skills",
            ):
                destination = private / name
                self.assertTrue(destination.is_symlink(), name)
                self.assertEqual(destination.resolve(), (source / name).resolve())
            self.assertEqual((private / "auth.json").read_text(), "private-auth\n")
            self.assertEqual((private / "current").read_text(), "work\n")
            self.assertEqual(
                (private / "state_5.sqlite").read_bytes(), b"private-state"
            )
            self.assertFalse((private / "auth.json").is_symlink())
            self.assertFalse((private / "state_5.sqlite").is_symlink())

    def test_wrapper_injects_private_sqlite_override_before_user_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private home's scope"
            launcher = root / "host bin" / "codex-real"
            record = root / "record.json"
            host_home = root / "host-home"
            source.mkdir()
            (source / "config.toml").write_text(
                'sqlite_home = "/canonical/state"\n', encoding="utf-8"
            )
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                status = prepare_shared_codex_configuration(private, source)

            self.assertIsNotNone(status.wrapper_path)
            wrapper = status.wrapper_path
            assert wrapper is not None
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(host_home),
                    "TERMDECK_TEST_CODEX_RECORD": str(record),
                }
            )
            subprocess.run(
                [str(wrapper), "--yolo", "resume", "019f-test"],
                check=True,
                env=environment,
            )
            observation = json.loads(record.read_text(encoding="utf-8"))

            self.assertEqual(
                observation["argv"],
                [
                    "-c",
                    f"sqlite_home={json.dumps(str(private.resolve()))}",
                    "--yolo",
                    "resume",
                    "019f-test",
                ],
            )
            self.assertEqual(observation["codex_home"], str(private.resolve()))
            self.assertEqual(
                observation["codex_termdeck_gate_root"],
                str(private.resolve() / ".termdeck-codex-lifetime"),
            )
            self.assertEqual(observation["home"], str(host_home))
            self.assertEqual(
                stat.S_IMODE(wrapper.stat().st_mode),
                0o700,
            )

    def test_wrapper_rejects_a_user_sqlite_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            record = root / "record.json"
            source.mkdir()
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                status = prepare_shared_codex_configuration(private, source)
            assert status.wrapper_path is not None
            environment = os.environ.copy()
            environment["TERMDECK_TEST_CODEX_RECORD"] = str(record)

            for override_args in (
                ("-c", 'sqlite_home="/shared"'),
                ('--config=sqlite_home="/shared"',),
                ('-c=sqlite_home="/shared"',),
            ):
                result = subprocess.run(
                    [str(status.wrapper_path), *override_args, "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(result.returncode, 64)
                self.assertIn("sqlite_home is managed by TermDeck", result.stderr)
                self.assertFalse(record.exists())

    def test_existing_legacy_configuration_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (source / "skills").mkdir()
            (private / "config.toml").write_text("legacy\n", encoding="utf-8")
            (private / "skills").mkdir()
            (private / "skills" / "local-skill").write_text(
                "must survive\n", encoding="utf-8"
            )
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                status = prepare_shared_codex_configuration(private, source)

            self.assertFalse(status.ready)
            self.assertTrue(status.migration_required)
            self.assertEqual(
                set(status.migration_required_paths),
                {private / "config.toml", private / "skills"},
            )
            self.assertFalse((private / "config.toml").is_symlink())
            self.assertEqual((private / "config.toml").read_text(), "legacy\n")
            self.assertEqual(
                (private / "skills" / "local-skill").read_text(),
                "must survive\n",
            )
            self.assertIsNotNone(status.wrapper_path)

    def test_quiescent_migration_backs_up_legacy_assets_and_reaches_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical config\n", encoding="utf-8")
            (source / "skills").mkdir()
            (source / "plugins").mkdir()
            (private / "config.toml").write_text("legacy config\n", encoding="utf-8")
            (private / "skills").mkdir()
            (private / "skills" / "private-skill").write_text(
                "legacy skill\n",
                encoding="utf-8",
            )
            (private / "plugins").mkdir()
            (private / "plugins" / "private-plugin").write_text(
                "legacy plugin\n",
                encoding="utf-8",
            )
            (private / "private.config.toml").write_text(
                "legacy profile\n",
                encoding="utf-8",
            )
            (private / "auth.json").write_text("private auth\n", encoding="utf-8")
            custom_wrapper = private / "bin" / "codex"
            custom_wrapper.parent.mkdir()
            custom_wrapper.write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
            custom_wrapper.chmod(0o700)
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                before = prepare_shared_codex_configuration(private, source)
                result = migrate_quiescent_codex_configuration(private)
                repeated = migrate_quiescent_codex_configuration(private)

            assert result is not None
            assert repeated is not None
            self.assertFalse(before.ready)
            self.assertTrue(result.status.ready)
            self.assertIsNotNone(result.backup_path)
            backup = result.backup_path
            assert backup is not None
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
            self.assertEqual(
                (backup / "config.toml").read_text(encoding="utf-8"),
                "legacy config\n",
            )
            self.assertEqual(
                (backup / "skills" / "private-skill").read_text(encoding="utf-8"),
                "legacy skill\n",
            )
            self.assertEqual(
                (backup / "plugins" / "private-plugin").read_text(encoding="utf-8"),
                "legacy plugin\n",
            )
            self.assertEqual(
                (backup / "private.config.toml").read_text(encoding="utf-8"),
                "legacy profile\n",
            )
            self.assertEqual(
                (backup / "bin" / "codex").read_text(encoding="utf-8"),
                "#!/bin/sh\necho legacy\n",
            )
            for name in ("config.toml", "skills", "plugins"):
                self.assertTrue((private / name).is_symlink(), name)
                self.assertEqual((private / name).resolve(), (source / name).resolve())
            self.assertFalse((private / "private.config.toml").exists())
            self.assertEqual(
                (private / "auth.json").read_text(encoding="utf-8"),
                "private auth\n",
            )
            self.assertTrue((private / "bin" / "codex").is_file())
            self.assertIsNone(repeated.backup_path)
            self.assertEqual(repeated.migrated_paths, ())
            self.assertEqual(
                repeated.status.shared_paths,
                result.status.shared_paths,
            )

    def test_quiescent_migration_rolls_back_every_legacy_path_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (private / "config.toml").write_text("legacy\n", encoding="utf-8")
            custom_wrapper = private / "bin" / "codex"
            custom_wrapper.parent.mkdir()
            custom_wrapper.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
            custom_wrapper.chmod(0o700)
            _write_launcher(launcher)
            original_write_wrapper = __import__(
                "termdeck.codex_config_scope",
                fromlist=["_write_wrapper"],
            )._write_wrapper

            def publish_then_fail(path: Path, content: str) -> None:
                original_write_wrapper(path, content)
                raise CodexConfigScopeError("simulated publication failure")

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                prepare_shared_codex_configuration(private, source)
                with mock.patch(
                    "termdeck.codex_config_scope._write_wrapper",
                    side_effect=publish_then_fail,
                ):
                    with self.assertRaisesRegex(
                        CodexConfigScopeError,
                        "simulated publication failure",
                    ):
                        migrate_quiescent_codex_configuration(private)

            self.assertFalse((private / "config.toml").is_symlink())
            self.assertEqual(
                (private / "config.toml").read_text(encoding="utf-8"),
                "legacy\n",
            )
            self.assertEqual(
                custom_wrapper.read_text(encoding="utf-8"),
                "#!/bin/sh\necho custom\n",
            )
            backup_root = private / ".termdeck-config-backups"
            self.assertFalse(backup_root.exists())

    def test_canonical_binding_persists_and_rejects_a_different_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "first-canonical"
            second_source = root / "second-canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            first_source.mkdir()
            second_source.mkdir()
            (first_source / "config.toml").write_text("first\n", encoding="utf-8")
            (second_source / "config.toml").write_text("second\n", encoding="utf-8")
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                prepare_shared_codex_configuration(private, first_source)
                with self.assertRaisesRegex(
                    CodexConfigScopeError,
                    "canonical-home binding has an unexpected target",
                ):
                    prepare_shared_codex_configuration(private, second_source)

            binding = private / ".termdeck-canonical-home"
            self.assertTrue(binding.is_symlink())
            self.assertEqual(binding.resolve(), first_source.resolve())
            self.assertEqual(
                (private / "config.toml").resolve(),
                (first_source / "config.toml").resolve(),
            )

    def test_migration_rejects_a_broken_persisted_canonical_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (private / "config.toml").write_text("legacy\n", encoding="utf-8")
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                prepare_shared_codex_configuration(private, source)
                for child in source.iterdir():
                    child.unlink()
                source.rmdir()
                with self.assertRaisesRegex(
                    CodexConfigScopeError,
                    "canonical-home binding cannot be resolved",
                ):
                    migrate_quiescent_codex_configuration(private)

            self.assertEqual(
                (private / "config.toml").read_text(encoding="utf-8"),
                "legacy\n",
            )
            self.assertFalse((private / ".termdeck-config-backups").exists())

    def test_quiescent_migration_rechecks_legacy_types_under_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (private / "config.toml").write_text("legacy\n", encoding="utf-8")
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                prepare_shared_codex_configuration(private, source)
                (private / "config.toml").unlink()
                (private / "config.toml").mkdir()
                with self.assertRaisesRegex(
                    CodexConfigScopeError,
                    "not a regular file",
                ):
                    migrate_quiescent_codex_configuration(private)

            self.assertTrue((private / "config.toml").is_dir())
            self.assertFalse((private / ".termdeck-config-backups").exists())

    def test_existing_custom_wrapper_is_preserved_and_requires_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            wrapper = private / "bin" / "codex"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
            wrapper.chmod(0o700)
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                status = prepare_shared_codex_configuration(private, source)

            self.assertFalse(status.ready)
            self.assertEqual(status.migration_required_paths, (wrapper,))
            self.assertIsNone(status.wrapper_path)
            self.assertEqual(wrapper.read_text(), "#!/bin/sh\necho custom\n")

    def test_unexpected_configuration_link_fails_before_any_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            unexpected = root / "unexpected.toml"
            launcher = root / "bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (source / "AGENTS.md").write_text("guidance\n", encoding="utf-8")
            unexpected.write_text("unexpected\n", encoding="utf-8")
            (private / "config.toml").symlink_to(unexpected)
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                with self.assertRaises(CodexConfigScopeError):
                    prepare_shared_codex_configuration(private, source)

            self.assertFalse((private / "AGENTS.md").exists())
            self.assertFalse((private / "bin").exists())
            self.assertEqual((private / "config.toml").resolve(), unexpected)

    def test_launcher_resolution_skips_an_existing_termdeck_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            parent = root / "parent"
            child = root / "child"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                parent_status = prepare_shared_codex_configuration(parent, source)
            assert parent_status.wrapper_path is not None

            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = resolve_codex_launcher(
                    child,
                    search_path=str(parent_status.wrapper_path.parent),
                    user_home=root / "empty-user-home",
                )

            if resolved is not None:
                self.assertNotEqual(resolved, parent_status.wrapper_path.resolve())
                self.assertNotIn(parent.resolve(), resolved.parents)

    def test_explicit_private_wrapper_launcher_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            parent = root / "parent"
            child = root / "child"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            _write_launcher(launcher)
            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                parent_status = prepare_shared_codex_configuration(parent, source)
            assert parent_status.wrapper_path is not None

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(parent_status.wrapper_path)},
            ):
                with self.assertRaises(CodexConfigScopeError):
                    prepare_shared_codex_configuration(child, source)

            self.assertFalse(child.exists())

    def test_user_local_launcher_precedes_a_desktop_system_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            user_launcher = root / "user" / ".local" / "bin" / "codex"
            system_launcher = root / "system-bin" / "codex"
            _write_launcher(user_launcher)
            _write_launcher(system_launcher)

            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = resolve_codex_launcher(
                    private,
                    search_path=str(system_launcher.parent),
                    user_home=root / "user",
                    launcher_attestor=_attest_test_launcher,
                )

            self.assertEqual(resolved, user_launcher.resolve())

    def test_managed_launcher_attestation_matches_shipped_helper_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_home = root / "user"
            private = root / "private"
            bin_dir = user_home / ".local" / "bin"
            libexec_dir = user_home / ".local" / "libexec"
            bin_dir.mkdir(parents=True)
            libexec_dir.mkdir(parents=True)
            private.mkdir()
            launcher = bin_dir / "codex-contained-user"
            entry = bin_dir / "codex"
            helper = libexec_dir / "codex-contained-user.sh"
            launcher.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                f"exec {helper} \"$@\"\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            entry.symlink_to(launcher)
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                "resolver=\"${CODEX_CANONICAL_WORKSPACE_HELPER:-resolver}\"\n"
                "scope_helper=\"${CODEX_RUNTIME_SCOPE_HELPER:-scope}\"\n"
                "vendor_root=\"${CODEX_VENDOR_RUNTIME_ROOT:-vendor}\"\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            with (
                mock.patch(
                    "termdeck.codex_config_scope.pwd.getpwuid",
                    return_value=SimpleNamespace(pw_dir=str(user_home)),
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                resolved = resolve_codex_launcher(private, user_home=user_home)
            self.assertEqual(resolved, launcher.resolve())

    def test_unattested_executable_named_codex_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            launcher = root / "untrusted-bin" / "codex"
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = resolve_codex_launcher(
                    private,
                    search_path=str(launcher.parent),
                    user_home=root / "empty-user-home",
                )

            self.assertIsNone(resolved)

    def test_unattested_explicit_launcher_override_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            launcher = root / "untrusted-bin" / "codex"
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
                clear=True,
            ):
                with self.assertRaisesRegex(
                    CodexConfigScopeError,
                    "not an attested managed Codex launcher",
                ):
                    resolve_codex_launcher(private, user_home=root / "user")

    def test_explicit_test_attestation_accepts_fixture_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            launcher = root / "fixture-bin" / "codex"
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
                clear=True,
            ):
                resolved = resolve_codex_launcher(
                    private,
                    user_home=root / "user",
                    launcher_attestor=_attest_test_launcher,
                )

            self.assertEqual(resolved, launcher.resolve())

    def test_symlinked_wrapper_directory_is_rejected_without_asset_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            unexpected_bin = root / "unexpected-bin"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            unexpected_bin.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (private / "bin").symlink_to(unexpected_bin, target_is_directory=True)
            _write_launcher(launcher)

            with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}):
                with self.assertRaises(CodexConfigScopeError):
                    prepare_shared_codex_configuration(private, source)

            self.assertFalse((private / "config.toml").exists())
            self.assertEqual(list(unexpected_bin.iterdir()), [])

    def test_private_home_symlink_alias_to_canonical_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            alias = root / "private-alias"
            source.mkdir()
            config = source / "config.toml"
            config.write_text("canonical\n", encoding="utf-8")
            alias.symlink_to(source, target_is_directory=True)

            with self.assertRaises(CodexConfigScopeError):
                prepare_shared_codex_configuration(alias, source)

            self.assertEqual(config.read_text(), "canonical\n")
            self.assertFalse((source / "bin").exists())

    def test_regular_file_private_home_uses_config_scope_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            source.mkdir()
            private.write_text("not a directory\n", encoding="utf-8")

            with self.assertRaises(CodexConfigScopeError):
                prepare_shared_codex_configuration(private, source)

            self.assertEqual(private.read_text(), "not a directory\n")

    def test_absent_canonical_home_is_an_explicit_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "absent-canonical"
            private = root / "absent-private"

            status = prepare_shared_codex_configuration(private, source)

            self.assertFalse(status.ready)
            self.assertFalse(status.canonical_available)
            self.assertFalse(status.migration_required)
            self.assertEqual(status.canonical_home, source.resolve(strict=False))
            self.assertEqual(status.private_home, private.resolve(strict=False))
            self.assertFalse(private.exists())

    def test_non_directory_canonical_home_is_an_explicit_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "not-a-canonical-home"
            private = root / "private"
            source.write_text("not a directory\n", encoding="utf-8")

            status = prepare_shared_codex_configuration(private, source)

            self.assertFalse(status.ready)
            self.assertFalse(status.canonical_available)
            self.assertEqual(status.canonical_home, source.resolve())
            self.assertFalse(private.exists())

    def test_broken_canonical_home_symlink_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "broken-canonical"
            private = root / "private"
            source.symlink_to(root / "missing-target", target_is_directory=True)

            with self.assertRaises(CodexConfigScopeError):
                prepare_shared_codex_configuration(private, source)

            self.assertFalse(private.exists())

    def test_canonical_home_symlink_alias_to_private_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            alias = root / "canonical-alias"
            private.mkdir()
            alias.symlink_to(private, target_is_directory=True)

            with self.assertRaises(CodexConfigScopeError):
                prepare_shared_codex_configuration(private, alias)

            self.assertFalse((private / "bin").exists())

    def test_canonical_home_alias_through_a_symlinked_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            alias_parent = root / "alias-parent"
            private.mkdir()
            alias_parent.symlink_to(root, target_is_directory=True)

            with self.assertRaises(CodexConfigScopeError):
                prepare_shared_codex_configuration(
                    private,
                    alias_parent / private.name,
                )

            self.assertFalse((private / "bin").exists())

    def test_distinct_symlinked_canonical_home_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical-target"
            source_alias = root / "canonical-link"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            source_alias.symlink_to(source, target_is_directory=True)
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                status = prepare_shared_codex_configuration(private, source_alias)

            self.assertTrue(status.ready)
            self.assertEqual(status.canonical_home, source.resolve())
            self.assertEqual(
                (private / "config.toml").resolve(),
                (source / "config.toml").resolve(),
            )

    def test_canonical_assets_must_match_their_declared_types(self):
        invalid_assets = (("config.toml", True), ("skills", False))
        for name, directory in invalid_assets:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "canonical"
                private = root / "private"
                launcher = root / "host-bin" / "codex"
                source.mkdir()
                if directory:
                    (source / name).mkdir()
                else:
                    (source / name).write_text("wrong type\n", encoding="utf-8")
                _write_launcher(launcher)

                with mock.patch.dict(
                    os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}
                ):
                    with self.assertRaises(CodexConfigScopeError):
                        prepare_shared_codex_configuration(private, source)

                self.assertFalse((private / name).exists())

    def test_symlinked_canonical_assets_must_resolve_to_declared_types(self):
        invalid_assets = (("config.toml", True), ("skills", False))
        for name, target_is_directory in invalid_assets:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "canonical"
                private = root / "private"
                launcher = root / "host-bin" / "codex"
                outside = root / "outside"
                source.mkdir()
                if target_is_directory:
                    outside.mkdir()
                else:
                    outside.write_text("wrong type\n", encoding="utf-8")
                (source / name).symlink_to(
                    outside,
                    target_is_directory=target_is_directory,
                )
                _write_launcher(launcher)

                with mock.patch.dict(
                    os.environ,
                    {"TERMDECK_CODEX_BINARY": str(launcher)},
                ):
                    with self.assertRaises(CodexConfigScopeError):
                        prepare_shared_codex_configuration(private, source)

                self.assertFalse((private / name).exists())

    def test_canonical_assets_must_not_be_group_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            config = source / "config.toml"
            config.write_text("canonical\n", encoding="utf-8")
            config.chmod(0o660)
            _write_launcher(launcher)

            with mock.patch.dict(
                os.environ,
                {"TERMDECK_CODEX_BINARY": str(launcher)},
            ):
                with self.assertRaisesRegex(
                    CodexConfigScopeError,
                    "unsafe authority",
                ):
                    prepare_shared_codex_configuration(private, source)

            self.assertFalse((private / "config.toml").exists())

    def test_wrapper_failure_rolls_back_only_links_created_by_this_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            private.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            (source / "AGENTS.md").write_text("guidance\n", encoding="utf-8")
            (private / "AGENTS.md").symlink_to(source / "AGENTS.md")
            _write_launcher(launcher)

            with (
                mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}),
                mock.patch(
                    "termdeck.codex_config_scope._write_wrapper",
                    side_effect=CodexConfigScopeError("simulated wrapper failure"),
                ),
            ):
                with self.assertRaisesRegex(
                    CodexConfigScopeError, "simulated wrapper failure"
                ):
                    prepare_shared_codex_configuration(private, source)

            self.assertFalse((private / "config.toml").exists())
            self.assertTrue((private / "AGENTS.md").is_symlink())
            self.assertEqual(
                (private / "AGENTS.md").resolve(), (source / "AGENTS.md").resolve()
            )

    def test_concurrent_wrapper_failure_cannot_remove_another_calls_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            source.mkdir()
            (source / "config.toml").write_text("canonical\n", encoding="utf-8")
            _write_launcher(launcher)

            first_writer_entered = threading.Event()
            release_first_writer = threading.Event()
            second_writer_entered = threading.Event()
            results: list[object] = []
            original_write_wrapper = __import__(
                "termdeck.codex_config_scope",
                fromlist=["_write_wrapper"],
            )._write_wrapper

            def controlled_write_wrapper(path: Path, content: str) -> None:
                if threading.current_thread().name == "first-prepare":
                    first_writer_entered.set()
                    if not release_first_writer.wait(timeout=5):
                        raise AssertionError("timed out releasing the first writer")
                    raise CodexConfigScopeError("simulated first writer failure")
                second_writer_entered.set()
                original_write_wrapper(path, content)

            def prepare() -> None:
                try:
                    results.append(prepare_shared_codex_configuration(private, source))
                except Exception as exc:  # Capture worker-thread evidence.
                    results.append(exc)

            with (
                mock.patch.dict(
                    os.environ,
                    {"TERMDECK_CODEX_BINARY": str(launcher)},
                ),
                mock.patch(
                    "termdeck.codex_config_scope._write_wrapper",
                    side_effect=controlled_write_wrapper,
                ),
            ):
                first = threading.Thread(target=prepare, name="first-prepare")
                second = threading.Thread(target=prepare, name="second-prepare")
                first.start()
                self.assertTrue(first_writer_entered.wait(timeout=5))
                second.start()
                self.assertFalse(second_writer_entered.wait(timeout=0.2))
                release_first_writer.set()
                first.join(timeout=5)
                second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(results), 2)
            self.assertEqual(
                sum(isinstance(result, CodexConfigScopeError) for result in results),
                1,
            )
            self.assertTrue((private / "config.toml").is_symlink())
            self.assertEqual(
                (private / "config.toml").resolve(),
                (source / "config.toml").resolve(),
            )
            self.assertTrue((private / "bin" / "codex").is_file())

    def test_canonical_home_does_not_self_link_or_need_a_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "canonical"
            source.mkdir()
            config = source / "config.toml"
            config.write_text("canonical\n", encoding="utf-8")

            status = prepare_shared_codex_configuration(source, source)

            self.assertTrue(status.ready)
            self.assertTrue(status.canonical_home_in_use)
            self.assertFalse(config.is_symlink())
            self.assertIsNone(status.wrapper_path)


if __name__ == "__main__":
    unittest.main()
