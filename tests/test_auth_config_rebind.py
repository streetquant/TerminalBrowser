import argparse
import contextlib
import fcntl
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from termdeck import auth_config_rebind as rebind
from termdeck import codex_config_scope as config
from termdeck.cli import build_parser, cmd_auth_rebind_config
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import (
    TmuxBackend, TmuxError, _auth_legacy_lifetime_lock_path,
    _auth_lifetime_lock_path,
)


class AuthConfigurationRebindTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "system-codex"
        self.old_source = self.root / "absent-old-codex"
        self.source.mkdir(mode=0o700)
        (self.source / "config.toml").write_text("model = 'native'\n")
        (self.source / "skills").mkdir()
        (self.source / "plugins").mkdir()
        (self.source / "auth.json").write_bytes(b"global-credential-must-not-change")
        (self.source / "accounts").mkdir()
        (self.source / "accounts" / "current").write_bytes(b"global-current")
        self.launcher = self.root / "host-codex"
        self.launcher.write_text("#!/bin/sh\nexit 0\n")
        self.launcher.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "TERMDECK_CODEX_BINARY": str(self.launcher),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "CODEX_HOME": str(self.root / "unrelated-agent-home"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        Path(os.environ["CODEX_HOME"]).mkdir()
        (Path(os.environ["CODEX_HOME"]) / "auth.json").write_bytes(b"agent-private-auth")
        self.store = SessionStore(
            self.root / "sessions.sqlite3", account_source=self.source,
            provision_auth_scopes=False,
        )
        self.addCleanup(self.store.close)
        self.private, self.record = self._scope("one")
        self.neighbor, self.neighbor_record = self._scope("two")
        self.backend = TmuxBackend(executable="/usr/bin/tmux")
        self.exists = mock.patch.object(self.backend, "_auth_session_exists", return_value=False)
        self.exists_mock = self.exists.start()
        self.addCleanup(self.exists.stop)
        self.no_subprocess = mock.patch.object(
            self.backend, "_run", side_effect=AssertionError("migration cannot launch or control a terminal"),
        )
        self.no_subprocess.start()
        self.addCleanup(self.no_subprocess.stop)

    def _scope(self, name):
        private = self.root / f"auth-{name}"
        private.mkdir(mode=0o700)
        (private / config._CANONICAL_HOME_BINDING).symlink_to(self.old_source)
        for asset in ("config.toml", "skills", "retired.config.toml"):
            (private / asset).symlink_to(self.old_source / asset)
        (private / "bin").mkdir(mode=0o700)
        (private / "bin" / "codex").write_text(config._wrapper_content(private, self.launcher))
        (private / "bin" / "codex").chmod(0o700)
        (private / "auth.json").write_bytes(f"credential-{name}".encode())
        (private / "accounts").mkdir(mode=0o700)
        (private / "accounts" / "current").write_bytes(f"current-{name}".encode())
        (private / "accounts" / "one.json").write_bytes(f"saved-credential-{name}".encode())
        (private / "state_5.sqlite").write_bytes(b"private state bytes are not parsed or replaced")
        (private / "history.jsonl").write_bytes(b"private history bytes")
        for history in ("sessions", "archived_sessions"):
            (private / history).symlink_to(self.old_source / history)
        record = self.store.create(
            name, "saved", "/usr/bin/bash", str(self.root),
            window_id="preserved-window", window_title="Saved", tab_index=7,
        )
        # Represent an existing legacy record; current provisioning must not run.
        self.store.conn.execute(
            """UPDATE sessions SET auth_scope_id=?, codex_home=?, status='missing',
               recovery_intent='active', selected_account='saved-account',
               pending_account='pending-account', auth_restart_required=1,
               auth_changed_at='saved-auth-time', recovery_attempts=3,
               recovery_last_error='saved error' WHERE id=?""",
            (private.name, str(private), record.id),
        )
        self.store.conn.commit()
        return private, self.store.get(record.id)

    def _migrate(self, record=None, **kwargs):
        return rebind.rebind_terminal_configuration(
            record or self.record, self.store, self.backend,
            expected_source=kwargs.pop("expected_source", self.old_source),
            source=kwargs.pop("source", self.source),
            refresh_launcher=kwargs.pop("refresh_launcher", True),
            launcher_attestor=lambda candidate: candidate == self.launcher,
            **kwargs,
        )

    def _rows(self):
        return [tuple(row) for row in self.store.conn.execute("SELECT * FROM sessions ORDER BY id")]

    def _tree(self, path):
        result = {}
        for child in sorted(path.rglob("*")):
            name = str(child.relative_to(path))
            node = child.lstat()
            if child.is_symlink():
                value = ("link", os.readlink(child))
            elif child.is_file():
                value = ("file", child.read_bytes())
            else:
                value = ("directory",)
            result[name] = (stat.S_IMODE(node.st_mode), value)
        return result

    def _protected(self):
        return {
            name: self._tree(self.private / name) if (self.private / name).is_dir()
            else os.readlink(self.private / name) if (self.private / name).is_symlink()
            else (self.private / name).read_bytes()
            for name in ("auth.json", "accounts", "state_5.sqlite", "history.jsonl", "sessions", "archived_sessions")
        }

    def _old_links(self):
        for name in (config._CANONICAL_HOME_BINDING, "config.toml", "skills", "retired.config.toml"):
            expected = self.old_source if name == config._CANONICAL_HOME_BINDING else self.old_source / name
            self.assertEqual(os.readlink(self.private / name), str(expected))
            self.assertEqual((self.private / name).lstat().st_nlink, 1)

    def test_generic_shell_rebind_preserves_all_rows_credentials_history_and_neighbors(self):
        before = self._rows(), self._protected(), self._tree(self.neighbor), self._tree(self.source)
        agent_before = self._tree(Path(os.environ["CODEX_HOME"]))
        result = self._migrate()
        self.assertEqual(result["source"], str(self.source))
        self.assertTrue(result["session_fields_preserved"])
        self.assertFalse(result["terminal_started"])
        self.assertEqual(before, (self._rows(), self._protected(), self._tree(self.neighbor), self._tree(self.source)))
        self.assertEqual(agent_before, self._tree(Path(os.environ["CODEX_HOME"])))
        self.assertEqual((self.private / config._CANONICAL_HOME_BINDING).resolve(), self.source)
        for asset in ("config.toml", "skills", "plugins"):
            self.assertEqual((self.private / asset).resolve(), self.source / asset)
        self.assertFalse((self.private / "retired.config.toml").is_symlink())
        self.assertIn(config._WRAPPER_MARKER, (self.private / "bin" / "codex").read_text())
        backup = Path(result["backup_path"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(os.readlink(backup / config._CANONICAL_HOME_BINDING), str(self.old_source))
        self.assertEqual((backup / "codex-launcher").read_text(), config._wrapper_content(self.private, self.launcher))
        self.assertFalse((self.root / "terminal-auth").exists())

    def test_read_only_inspection_makes_no_directories_or_lock_files(self):
        before = self._tree(self.root)
        plan = rebind.inspect_configuration_rebind(
            self.private, expected_source=self.old_source, source=self.source,
            launcher_attestor=lambda candidate: candidate == self.launcher,
        )
        self.assertEqual(plan.private_home, self.private)
        self.assertEqual(before, self._tree(self.root))

    def test_default_preserves_exact_private_launcher_without_broadening_attestation(self):
        wrapper = self.private / "bin" / "codex"
        before = wrapper.read_bytes(), wrapper.lstat(), self._protected(), self._rows()
        with mock.patch.object(config, "resolve_codex_launcher", side_effect=AssertionError("must preserve without attesting")):
            result = self._migrate(refresh_launcher=False)
        self.assertEqual(before, (wrapper.read_bytes(), wrapper.lstat(), self._protected(), self._rows()))
        self.assertTrue(result["launcher_preserved"])
        self.assertFalse(result["launcher_qualified"])
        self.assertNotIn("bin/codex", result["replaced_paths"])

    def test_preservation_rejects_unknown_wrapper_and_refresh_still_requires_attestation(self):
        wrapper = self.private / "bin" / "codex"
        wrapper.write_text("#!/bin/sh\necho unrecognized\n")
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "recognized managed wrapper"):
            self._migrate(refresh_launcher=False)
        with mock.patch.object(config, "resolve_codex_launcher", return_value=None):
            with self.assertRaisesRegex(rebind.ConfigurationRebindError, "attested host"):
                self._migrate(refresh_launcher=True)
        self._old_links()

    def test_preservation_refuses_a_wrapper_bound_to_a_neighboring_home(self):
        (self.private / "bin" / "codex").write_text(config._wrapper_content(self.neighbor, self.launcher))
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "recognized managed wrapper"):
            self._migrate(refresh_launcher=False)
        self._old_links()

    def test_symlink_private_home_is_refused_without_following_it(self):
        alias = self.root / "alias" / self.private.name
        alias.parent.mkdir()
        alias.symlink_to(self.private)
        before = self._tree(self.private)
        with self.assertRaisesRegex(config.CodexConfigScopeError, "unsafe authority"):
            self._migrate(replace(self.record, codex_home=str(alias)))
        self.assertEqual(before, self._tree(self.private))

    def test_replay_with_old_expected_binding_refuses_without_changing_success(self):
        self._migrate()
        before = self._tree(self.private), self._rows()
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "expected source"):
            self._migrate()
        self.assertEqual(before, (self._tree(self.private), self._rows()))

    def test_inactive_saved_intent_is_preserved_without_starting(self):
        record = self.store.update(self.record.id, status="new", recovery_intent="inactive")
        before = self._rows()
        self._migrate(record)
        self.assertEqual(before, self._rows())
        self.assertEqual(self.store.get(record.id).recovery_intent, "inactive")

    def test_missing_private_home_is_never_provisioned_by_either_migration_path(self):
        missing = self.root / "missing-parent" / "auth-missing"
        record = replace(self.record, codex_home=str(missing), auth_scope_id=missing.name)
        with self.assertRaisesRegex(config.CodexConfigScopeError, "missing.*not be recreated"):
            self._migrate(record)
        with self.assertRaisesRegex(config.CodexConfigScopeError, "missing.*not be recreated"):
            config.migrate_quiescent_codex_configuration(missing)
        with self.assertRaises(TmuxError):
            self.backend.create_or_resume(record)
        self.assertFalse(missing.parent.exists())
        self.exists_mock.assert_not_called()

    def test_live_tmux_refuses_without_configuration_changes(self):
        before = self._protected(), self._rows()
        self.exists_mock.return_value = True
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "still active"):
            self._migrate()
        self._old_links()
        self.assertEqual(before, (self._protected(), self._rows()))
        self.assertFalse((self.private / config._CONFIG_BACKUP_ROOT).exists())

    def test_saved_live_process_refuses_even_if_backend_has_no_tmux(self):
        record = self.store.update(self.record.id, last_pid=os.getpid())
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "process is still present"):
            self._migrate(record)
        self.exists_mock.assert_not_called()
        self._old_links()

    def test_active_lifetime_writer_refuses(self):
        for lock_path in (_auth_legacy_lifetime_lock_path(self.record), _auth_lifetime_lock_path(self.record)):
            with self.subTest(generation=lock_path.parent.name):
                lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(TmuxError, "still owns"):
                        self._migrate()
                    self._old_links()
                finally:
                    os.close(descriptor)

    def test_both_lifetime_fences_and_registry_lock_are_held_during_publication(self):
        original = rebind._publish_wrapper

        def check_fences(descriptor, content):
            for path in (_auth_legacy_lifetime_lock_path(self.record), _auth_lifetime_lock_path(self.record)):
                holder = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(holder)
            other = sqlite3.connect(self.store.path, timeout=0.01)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
            finally:
                other.close()
            original(descriptor, content)

        with mock.patch.object(rebind, "_publish_wrapper", side_effect=check_fences):
            self._migrate()
        self.assertFalse(self.store.conn.in_transaction)

    def test_changed_saved_row_before_fencing_refuses_without_repairing_it(self):
        record = self.record
        self.store.update(record.id, selected_account="changed-after-read")
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "saved terminal changed"):
            self._migrate(record)
        self._old_links()
        self.exists_mock.assert_not_called()

    def test_row_changed_during_quiescence_is_detected_under_registry_lock(self):
        def observe(_record):
            if self.exists_mock.call_count == 1:
                self.store.update(self.record.id, recovery_intent="inactive")
            return False
        self.exists_mock.side_effect = observe
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "saved terminal changed"):
            self._migrate()
        self._old_links()
        self.assertFalse((self.private / config._CONFIG_BACKUP_ROOT).exists())

    def test_shared_scope_binding_is_refused(self):
        # Exercise the guard against a legacy/corrupt registry without today's
        # unique indexes; normal SessionStore already rejects this state.
        self.store.conn.execute("DROP INDEX idx_sessions_auth_scope")
        self.store.conn.execute("DROP INDEX idx_sessions_codex_home")
        self.store.conn.execute("UPDATE sessions SET codex_home=?, auth_scope_id=? WHERE id=?",
                                (str(self.private), self.private.name, self.neighbor_record.id))
        self.store.conn.commit()
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "another saved terminal"):
            self._migrate()
        self._old_links()

    def test_unsafe_or_unexpected_sources_and_private_paths_refuse_before_fencing(self):
        cases = (
            ("relative source", lambda: self._migrate(source=Path("relative"))),
            ("wrong old binding", lambda: self._migrate(expected_source=self.root / "wrong")),
            ("same source", lambda: self._migrate(source=self.old_source)),
        )
        for name, operation in cases:
            with self.subTest(case=name), self.assertRaises((config.CodexConfigScopeError, OSError)):
                operation()
        self.source.chmod(0o777)
        with self.assertRaisesRegex(config.CodexConfigScopeError, "unsafe authority"):
            self._migrate()
        self.source.chmod(0o700)
        (self.private / "skills").unlink()
        (self.private / "skills").symlink_to(self.root / "unrelated")
        with self.assertRaisesRegex(rebind.ConfigurationRebindError, "expected source"):
            self._migrate()
        self.exists_mock.assert_not_called()

    def test_wrapper_publication_failure_restores_original_links_and_launcher(self):
        before = self._rows(), self._protected(), (self.private / "bin" / "codex").read_bytes()
        original = rebind._publish_wrapper

        def fail_after_publish(descriptor, content):
            original(descriptor, content)
            raise OSError("injected failure after publication")

        with mock.patch.object(rebind, "_publish_wrapper", side_effect=fail_after_publish):
            with self.assertRaisesRegex(rebind.ConfigurationRebindError, "rolled back"):
                self._migrate()
        self._old_links()
        self.assertEqual(before, (self._rows(), self._protected(), (self.private / "bin" / "codex").read_bytes()))
        self.assertFalse((self.private / config._CONFIG_BACKUP_ROOT).exists())

    def test_failure_after_binding_commit_restores_binding_atomically(self):
        before = self._rows(), self._protected(), (self.private / "bin" / "codex").read_bytes()
        with mock.patch.object(config, "_configuration_plan", side_effect=config.CodexConfigScopeError("injected final inspection")):
            with self.assertRaisesRegex(config.CodexConfigScopeError, "injected final inspection"):
                self._migrate()
        self._old_links()
        self.assertEqual(before, (self._rows(), self._protected(), (self.private / "bin" / "codex").read_bytes()))
        self.assertFalse((self.private / config._CONFIG_BACKUP_ROOT).exists())

    def test_source_changes_or_disappears_during_publication_roll_back_dangling_links(self):
        original = rebind._publish_wrapper
        for change in ("content", "root"):
            with self.subTest(change=change):
                def change_source(descriptor, content):
                    original(descriptor, content)
                    if change == "content":
                        (self.source / "config.toml").write_text("concurrent external edit\n")
                    else:
                        self.source.rename(self.root / "moved-system-codex")
                with mock.patch.object(rebind, "_publish_wrapper", side_effect=change_source):
                    with self.assertRaises(config.CodexConfigScopeError):
                        self._migrate()
                self._old_links()
                self.assertFalse((self.private / config._CONFIG_BACKUP_ROOT).exists())
                if change == "root":
                    (self.root / "moved-system-codex").rename(self.source)

    def test_changed_binding_is_not_overwritten_at_commit(self):
        original = rebind._publish_wrapper
        changed = self.root / "concurrent-binding"

        def change_binding(descriptor, content):
            original(descriptor, content)
            (self.private / config._CANONICAL_HOME_BINDING).unlink()
            (self.private / config._CANONICAL_HOME_BINDING).symlink_to(changed)

        with mock.patch.object(rebind, "_publish_wrapper", side_effect=change_binding):
            with self.assertRaisesRegex(rebind.ConfigurationRebindError, "binding changed"):
                self._migrate()
        self.assertEqual(os.readlink(self.private / config._CANONICAL_HOME_BINDING), str(changed))
        self.assertEqual(os.readlink(self.private / "config.toml"), str(self.old_source / "config.toml"))

    def test_rollback_does_not_overwrite_a_concurrent_configuration_destination(self):
        original = rebind._publish_wrapper
        concurrent = self.root / "concurrent-config"
        protected = self._protected(), self._tree(self.neighbor), self._rows()

        def change_destination(descriptor, content):
            original(descriptor, content)
            (self.private / "config.toml").unlink()
            (self.private / "config.toml").symlink_to(concurrent)
            raise OSError("injected concurrent publication")

        with mock.patch.object(rebind, "_publish_wrapper", side_effect=change_destination):
            with self.assertRaisesRegex(rebind.ConfigurationRebindError, "rollback evidence retained"):
                self._migrate()
        self.assertEqual(os.readlink(self.private / "config.toml"), str(concurrent))
        backups = list((self.private / config._CONFIG_BACKUP_ROOT).iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(os.readlink(backups[0] / "config.toml"), str(self.old_source / "config.toml"))
        self.assertEqual(protected, (self._protected(), self._tree(self.neighbor), self._rows()))

    def test_cli_requires_exact_id_and_explicit_source_without_scope_provisioning(self):
        parser = build_parser()
        args = parser.parse_args([
            "auth-rebind-config", self.record.id,
            "--expected-source", str(self.old_source), "--source", str(self.source),
            "--db", str(self.store.path),
        ])
        before = self._rows()
        output = io.StringIO()
        with mock.patch("termdeck.cli.TmuxBackend", return_value=self.backend), \
             mock.patch.object(config, "resolve_codex_launcher", return_value=self.launcher), \
             mock.patch.object(SessionStore, "_ensure_auth_scopes", side_effect=AssertionError("must not provision")), \
             contextlib.redirect_stdout(output):
            code = cmd_auth_rebind_config(args)
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(before, self._rows())
        self.assertFalse((self.root / "terminal-auth").exists())
        missing_args = argparse.Namespace(**{**vars(args), "session_id": self.record.id[:5]})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cmd_auth_rebind_config(missing_args), 1)
        self.assertIn("exact saved terminal ID", output.getvalue())


if __name__ == "__main__":
    unittest.main()
