"""Saved Bash recovery must preserve private state without launching Codex."""

import json
import os
import shlex
import shutil
import stat
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from termdeck import codex_config_scope as config
from termdeck.auth_config_rebind import rebind_terminal_configuration
from termdeck.checkpointing import DmtcpCheckpointManager
from termdeck.session_recipe import record_recipe_fields
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend, TmuxError


class PrivateBashRecoveryTests(unittest.TestCase):
    def setUp(self):
        # A short, private path also keeps the actual AF_UNIX socket in bounds.
        self.temporary = tempfile.TemporaryDirectory(prefix="td-bash-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "canonical"
        self.source.mkdir(mode=0o700)
        (self.source / "config.toml").write_text("model = 'fixture'\n")
        (self.source / "skills").mkdir(mode=0o700)
        (self.source / "auth.json").write_bytes(b"synthetic global credential")
        self.user_home = self.root / "user"
        self.user_home.mkdir(mode=0o700)
        (self.user_home / ".bashrc").write_text(
            "export PATH=/usr/bin:/bin\n"
            "export CODEX_HOME=/ignored-user-rc\n"
            "export TERMDECK_BASH_FIXTURE_RC=loaded\n"
        )
        self.socket_root = self.root / "tmux"
        self.socket_root.mkdir(mode=0o700)
        environment = dict(os.environ)
        for name in ("TMUX", "CODEX_HOME", "TERMDECK_CODEX_BINARY", "TERMDECK_CODEX_STATE_SOURCE"):
            environment.pop(name, None)
        environment.update({
            "HOME": str(self.user_home), "TMUX_TMPDIR": str(self.socket_root),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "TERMDECK_STATE_ROOT": str(self.root / "state"),
            "PATH": "/usr/bin:/bin", "TERM": "xterm-256color",
        })
        self.environment = mock.patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = SessionStore(
            self.root / "sessions.sqlite3", account_source=self.source,
            provision_auth_scopes=False,
        )
        self.addCleanup(self.store.close)
        self.private, self.record = self._scope("one")
        self.neighbor, self.neighbor_record = self._scope("two")
        self.backend = TmuxBackend(
            executable=shutil.which("tmux") or "/usr/bin/tmux",
            checkpoint_manager=DmtcpCheckpointManager(self.root / "checkpoints", enabled=False),
        )

    def _scope(self, name):
        private = self.root / f"auth-{name}"
        private.mkdir(mode=0o700)
        (private / config._CANONICAL_HOME_BINDING).symlink_to(self.source)
        for asset in ("config.toml", "skills"):
            (private / asset).symlink_to(self.source / asset)
        (private / "bin").mkdir(mode=0o700)
        # Intentionally unavailable: restoring Bash never executes this target.
        launcher = self.root / "absent-native-launcher"
        (private / "bin" / "codex").write_text(config._wrapper_content(private, launcher))
        (private / "bin" / "codex").chmod(0o700)
        (private / "auth.json").write_bytes(f"synthetic private {name}".encode())
        (private / "accounts").mkdir(mode=0o700)
        (private / "accounts" / "current").write_text(f"saved-{name}\n")
        (private / "state_5.sqlite").write_bytes(b"saved opaque state must not be hydrated")
        (private / "history.jsonl").write_bytes(b"saved private history\n")
        (private / "sessions").symlink_to(self.root / "absent-history" / name)
        working_directory = self.root / f"workspace-{name}"
        working_directory.mkdir()
        record = self.store.create(
            # Sanitized shape of all 76 existing saved production Bash recipes.
            f"saved-{name}", "saved-bash", "/bin/bash -i", str(working_directory),
            window_id="saved-window", window_title="Saved", tab_index=7,
        )
        self.store.conn.execute(
            """UPDATE sessions SET auth_scope_id=?, codex_home=?, status='missing',
               recovery_intent='active', selected_account='saved-account',
               pending_account='pending-account', auth_restart_required=1,
               auth_changed_at='saved-time', recovery_attempts=3,
               recovery_last_error='saved error' WHERE id=?""",
            (private.name, str(private), record.id),
        )
        self.store.conn.commit()
        return private, self.store.get(record.id)

    def _recipe(self, command, **changes):
        encoded, digest, policy = record_recipe_fields(
            command=command, cwd=self.record.cwd,
            restart_policy="on-failure", origin=changes.pop("origin", "launch"),
        )
        return replace(self.record, command=command, recovery_recipe=encoded,
                       recovery_recipe_sha256=digest, restart_policy=policy, **changes)

    def _rows(self):
        return [tuple(row) for row in self.store.conn.execute("SELECT * FROM sessions ORDER BY id")]

    def _tree(self, root):
        result = {}
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            value = ("link", os.readlink(path)) if path.is_symlink() else (
                ("file", path.read_bytes()) if path.is_file() else ("directory",)
            )
            result[str(path.relative_to(root))] = (
                metadata.st_ino, metadata.st_uid, stat.S_IMODE(metadata.st_mode), value,
            )
        return result

    def test_existing_scope_validation_is_read_only_and_does_not_attest_codex(self):
        before = self._tree(self.private)
        with mock.patch.object(config, "resolve_codex_launcher", side_effect=AssertionError("not a Codex launch")):
            self.assertEqual(config.validate_existing_private_bash_scope(
                self.private, auth_scope_id=self.private.name), self.private)
        self.assertEqual(self._tree(self.private), before)

    def test_direct_bash_skips_codex_migration_state_and_history_creation(self):
        (self.private / "state_5.sqlite").unlink()
        before = self._tree(self.private)
        with mock.patch("termdeck.tmux_backend.migrate_quiescent_codex_configuration") as migrate, mock.patch(
            "termdeck.tmux_backend.ensure_private_codex_state"
        ) as ensure:
            launch = self.backend._launch_spec(self.record)
        migrate.assert_not_called()
        ensure.assert_not_called()
        self.assertFalse(launch.checkpoint)
        self.assertFalse(launch.application_native_resume)
        self.assertIn("--rcfile", launch.command)
        after = self._tree(self.private)
        after.pop("bin/.termdeck-bashrc")
        self.assertEqual(after, before)
        self.assertFalse((self.private / "state_5.sqlite").exists())
        self.assertFalse((self.root / "absent-history").exists())

    def test_shell_spoofs_arguments_and_native_metadata_keep_codex_gate(self):
        impostor = self.root / "bash"
        impostor.write_text("#!/bin/sh\nexit 0\n")
        impostor.chmod(0o700)
        commands = (
            "bash", "/bin/bash -l", "/bin/bash -c true", "/bin/bash /tmp/script",
            "/bin/bash; true", "/usr/bin/env /bin/bash", str(impostor),
            "/bin/sh", "codex --yolo", "/bin/bash --norc", "/bin/bash && codex",
            "/bin/bash --rcfile /tmp/rc", "/bin/bash -ii", "/bin/bash -ix",
            "/bin/bash -i -c true", "/bin/bash\n-i",
        )
        cases = [self._recipe(command) for command in commands]
        cases.extend([
            self._recipe("/bin/bash", recovery_command="opaque resume"),
            self._recipe("/bin/bash", recovery_cwd=self.record.cwd),
            self._recipe("/bin/bash", origin="native-resume"),
            self._recipe("/bin/bash", origin="legacy-recovery"),
        ])
        for record in cases:
            with self.subTest(command=record.command, origin=record.recovery_recipe), mock.patch.object(
                config, "resolve_codex_launcher", return_value=None
            ), mock.patch("termdeck.tmux_backend.validate_existing_private_bash_scope") as validate, self.assertRaisesRegex(
                TmuxError, "no safe host Codex launcher"
            ):
                self.backend._launch_spec(record)
            validate.assert_not_called()

    def test_checkpoint_and_wrapped_launch_keep_codex_gate(self):
        for checkpoint in (True, False):
            with self.subTest(checkpoint=checkpoint), mock.patch.object(
                self.backend.checkpoints, "restart_command", return_value="checkpoint-restore" if checkpoint else None
            ), mock.patch.object(
                self.backend.checkpoints, "wrap_launch_command", return_value="checkpoint-wrap /bin/bash"
            ), mock.patch.object(config, "resolve_codex_launcher", return_value=None), mock.patch(
                "termdeck.tmux_backend.validate_existing_private_bash_scope"
            ) as validate, self.assertRaisesRegex(TmuxError, "no safe host Codex launcher"):
                self.backend._launch_spec(self.record)
            validate.assert_not_called()

    def test_configured_agent_does_not_use_bash_admission(self):
        record = replace(self.record, tag="web-agent", recovery_command="configured resume")
        def admit_exact_record(candidate):
            self.assertIs(candidate, record)
        with mock.patch("termdeck.agent_launch.validate_native_launch", side_effect=admit_exact_record) as admit, mock.patch(
            "termdeck.agent_launch.prepare_resume", side_effect=ValueError("configured policy rejected")
        ) as resume, mock.patch("termdeck.tmux_backend.validate_existing_private_bash_scope") as validate, mock.patch.object(
            self.backend, "_with_explicit_private_environment"
        ) as profile, mock.patch.object(self.backend, "_with_interactive_shell_fallback") as fallback, self.assertRaisesRegex(
            TmuxError, "configured policy rejected"
        ):
            self.backend._launch_spec(record)
        admit.assert_called_once_with(record)
        resume.assert_called_once_with(record)
        validate.assert_not_called()
        profile.assert_not_called()
        fallback.assert_not_called()

    def test_configured_admission_refusal_precedes_resume_profile_and_bash_fallback(self):
        from termdeck.client_files import StagingError

        record = replace(self.record, tag="web-agent", recovery_command="configured resume")
        with mock.patch("termdeck.agent_launch.validate_native_launch", side_effect=StagingError("configured admission rejected")) as admit, mock.patch(
            "termdeck.agent_launch.prepare_resume"
        ) as resume, mock.patch("termdeck.tmux_backend.validate_existing_private_bash_scope") as validate, mock.patch.object(
            self.backend, "_with_explicit_private_environment"
        ) as profile, mock.patch.object(self.backend, "_with_interactive_shell_fallback") as fallback, mock.patch.object(
            self.backend, "_run"
        ) as publish, self.assertRaisesRegex(TmuxError, "configured admission rejected"):
            self.backend._launch_spec(record)
        admit.assert_called_once_with(record)
        resume.assert_not_called()
        validate.assert_not_called()
        profile.assert_not_called()
        fallback.assert_not_called()
        publish.assert_not_called()

    def test_missing_home_and_bin_are_never_created(self):
        missing = self.root / "auth-missing"
        record = replace(self.record, auth_scope_id=missing.name, codex_home=str(missing))
        with self.assertRaises(TmuxError), mock.patch.object(self.backend, "_run") as run:
            self.backend.create_or_resume(record)
        run.assert_not_called()
        self.assertFalse(missing.exists())
        shutil.rmtree(self.private / "bin")
        with self.assertRaises((config.CodexConfigScopeError, OSError)):
            config.validate_existing_private_bash_scope(self.private, auth_scope_id=self.private.name)
        with self.assertRaises(config.CodexConfigScopeError):
            config.prepare_private_bash_launch_command(
                self.private, auth_scope_id=self.private.name, shell_path="/bin/bash",
                require_existing_scope=True,
            )
        self.assertFalse((self.private / "bin").exists())

    def test_unsafe_existing_bash_rc_is_rejected_without_reading_or_overwriting(self):
        rc = self.private / "bin" / ".termdeck-bashrc"
        for kind in ("fifo", "oversized", "hardlink", "symlink"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(rc, 0o600)
                elif kind == "oversized":
                    rc.write_bytes(b"x" * 65537)
                elif kind == "hardlink":
                    os.link(self.private / "auth.json", rc)
                else:
                    rc.symlink_to(self.private / "auth.json")
                before = rc.lstat()
                with self.assertRaisesRegex(TmuxError, "private Bash environment"):
                    self.backend._launch_spec(self.record)
                self.assertEqual(rc.lstat().st_ino, before.st_ino)
                rc.unlink()

    def test_unsafe_scope_authority_is_rejected_without_repair(self):
        def unsafe_home(home):
            home.chmod(0o777)

        def unsafe_bin(home):
            (home / "bin").chmod(0o777)

        def changed_wrapper(home):
            with (home / "bin" / "codex").open("a") as output:
                output.write("# forged change\n")

        def neighbor_wrapper(home):
            (home / "bin" / "codex").write_bytes((self.neighbor / "bin" / "codex").read_bytes())

        def unrelated_link(home):
            (home / "config.toml").unlink()
            (home / "config.toml").symlink_to(self.neighbor / "auth.json")

        def copied_config(home):
            (home / "config.toml").unlink()
            (home / "config.toml").write_text("model = 'private-copy'\n")

        def missing_binding(home):
            (home / config._CANONICAL_HOME_BINDING).unlink()

        def hardlinked_wrapper(home):
            os.link(home / "bin" / "codex", home / "other-wrapper")

        for index, mutate in enumerate((unsafe_home, unsafe_bin, changed_wrapper, neighbor_wrapper,
                                        unrelated_link, copied_config, missing_binding, hardlinked_wrapper)):
            with self.subTest(case=mutate.__name__):
                home, record = self._scope(f"unsafe{index}")
                mutate(home)
                before = self._tree(home)
                with self.assertRaises((config.CodexConfigScopeError, OSError)):
                    config.validate_existing_private_bash_scope(home, auth_scope_id=record.auth_scope_id)
                self.assertEqual(self._tree(home), before)

    def test_changed_binding_and_unsafe_canonical_source_are_rejected(self):
        original = config.preserved_private_launcher

        def swap_binding(home):
            value = original(home)
            (home / config._CANONICAL_HOME_BINDING).unlink()
            (home / config._CANONICAL_HOME_BINDING).symlink_to(self.neighbor)
            return value

        with mock.patch.object(config, "preserved_private_launcher", side_effect=swap_binding), self.assertRaises(
            config.CodexConfigScopeError
        ):
            config.validate_existing_private_bash_scope(self.private, auth_scope_id=self.private.name)
        (self.private / config._CANONICAL_HOME_BINDING).unlink()
        (self.private / config._CANONICAL_HOME_BINDING).symlink_to(self.source)
        (self.source / "config.toml").chmod(0o666)
        with self.assertRaisesRegex(config.CodexConfigScopeError, "unsafe authority"):
            config.validate_existing_private_bash_scope(self.private, auth_scope_id=self.private.name)

    @unittest.skipUnless(shutil.which("tmux"), "real tmux is required")
    def test_public_backend_refuses_unsafe_scope_and_command_spoof_without_spawn(self):
        wrapper = self.private / "bin" / "codex"
        saved = wrapper.read_bytes()
        wrapper.write_bytes(saved + b"# altered wrapper\n")
        rows = self._rows()
        with mock.patch.object(self.backend, "_run", wraps=self.backend._run) as run, self.assertRaisesRegex(
            TmuxError, "managed wrapper"
        ):
            self.backend.create_or_resume(self.record)
        self.assertNotIn("new-session", [call.args[0][0] for call in run.call_args_list])
        wrapper.write_bytes(saved)
        impostor = self.root / "bash"
        impostor.write_text("#!/bin/sh\nexit 0\n")
        impostor.chmod(0o700)
        with mock.patch.object(self.backend, "_run", wraps=self.backend._run) as run, mock.patch.object(
            config, "resolve_codex_launcher", return_value=None
        ), self.assertRaisesRegex(TmuxError, "no safe host Codex launcher"):
            self.backend.create_or_resume(self._recipe(str(impostor)))
        self.assertNotIn("new-session", [call.args[0][0] for call in run.call_args_list])
        self.assertFalse(self.backend._auth_session_exists(self.record))
        self.assertFalse((self.socket_root / f"tmux-{os.geteuid()}" / "default").exists())
        self.assertEqual(self._rows(), rows)

    @unittest.skipUnless(shutil.which("tmux"), "real tmux is required")
    def test_public_backend_restores_real_bash_on_its_own_socket_and_preserves_saved_identity(self):
        old_source = self.root / "absent-old-canonical"
        for name in (config._CANONICAL_HOME_BINDING, "config.toml", "skills"):
            (self.private / name).unlink()
            (self.private / name).symlink_to(old_source if name == config._CANONICAL_HOME_BINDING else old_source / name)
        rows = self._rows()
        source = self._tree(self.source)
        neighbor = self._tree(self.neighbor)
        preserved = {name: (self.private / name).read_bytes() for name in (
            "auth.json", "accounts/current", "state_5.sqlite", "history.jsonl", "bin/codex"
        )}
        history_link = os.readlink(self.private / "sessions")
        result = rebind_terminal_configuration(
            self.record, self.store, self.backend,
            expected_source=old_source, source=self.source,
        )
        self.assertTrue(result["configuration_ready"])
        self.assertTrue(result["launcher_preserved"])
        self.assertFalse(result["launcher_qualified"])
        self.assertEqual(self._rows(), rows)
        self.assertFalse(self.backend._auth_session_exists(self.record))
        try:
            self.backend.create_or_resume(self.record)
            deadline = time.monotonic() + 10
            pid = None
            while time.monotonic() < deadline:
                pid = self.backend.pane_pid(self.record)
                if pid and Path(f"/proc/{pid}/comm").read_text().strip() == "bash":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(pid)
            self.assertEqual(Path(f"/proc/{pid}/comm").read_text().strip(), "bash")
            self.assertEqual(Path(f"/proc/{pid}/cwd").resolve(), Path(self.record.cwd))
            self.assertEqual(self.backend.list_session_names(), [self.record.tmux_name])
            socket = self.socket_root / f"tmux-{os.geteuid()}" / "default"
            self.assertTrue(stat.S_ISSOCK(socket.stat().st_mode))
            pane = self.backend.list_panes_text("#{session_name}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_path}").strip()
            self.assertEqual(pane, f"{self.record.tmux_name}\t{pid}\t0\t{self.record.cwd}")
            self.backend.create_or_resume(self.record)
            self.assertEqual(self.backend.pane_pid(self.record), pid)
            self.assertEqual(self.backend.list_session_names(), [self.record.tmux_name])
            output = self.root / "own-shell-proof.json"
            program = (
                "import json,os;from pathlib import Path;"
                f"Path({str(output)!r}).write_text(json.dumps({{"
                "'cwd':os.getcwd(),'parent':os.getppid(),"
                "'environment':{k:os.environ.get(k) for k in "
                "('CODEX_HOME','TERMDECK_AUTH_SCOPE_ID','CODEX_TERMDECK_GATE_ROOT','PATH','TERMDECK_BASH_FIXTURE_RC')}}))"
            )
            self.backend.send_text(self.record, shlex.join(["/usr/bin/python3", "-c", program]))
            deadline = time.monotonic() + 10
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            observed = json.loads(output.read_text())
            self.assertEqual(observed["cwd"], self.record.cwd)
            self.assertEqual(observed["parent"], pid)
            self.assertEqual(observed["environment"], {
                "CODEX_HOME": str(self.private), "TERMDECK_AUTH_SCOPE_ID": self.private.name,
                "CODEX_TERMDECK_GATE_ROOT": str(self.private / ".termdeck-codex-lifetime"),
                "PATH": f"{self.private / 'bin'}:/usr/bin:/bin", "TERMDECK_BASH_FIXTURE_RC": "loaded",
            })
            self.assertEqual(self._rows(), rows)
            self.assertEqual(self.store.get(self.record.id), self.record)
            self.assertEqual(self._tree(self.source), source)
            self.assertEqual(self._tree(self.neighbor), neighbor)
            self.assertEqual(os.readlink(self.private / "sessions"), history_link)
            self.assertFalse((self.root / "absent-history").exists())
            for name, value in preserved.items():
                self.assertEqual((self.private / name).read_bytes(), value, name)
            self.runtime_receipt = {
                "session_id": self.record.id, "tmux_name": self.record.tmux_name,
                "socket": str(socket), "pid": pid, "cwd": observed["cwd"],
                "process": "bash", "pane_dead": False,
                "environment": observed["environment"],
                "repeat_same_pid": True, "public_send_text_observed": True,
                "saved_rows_unchanged": True, "source_unchanged": True,
                "neighbor_unchanged": True, "saved_private_files_unchanged": True,
                "missing_history_not_fabricated": True,
                "sanitized_saved_recipe": "/bin/bash -i",
            }
        finally:
            # The exact disposable name is the only permitted termination target.
            self.backend.terminate(self.record)
        self.assertFalse(self.backend._auth_session_exists(self.record))
        self.runtime_receipt["own_fixture_cleanup_verified"] = True


if __name__ == "__main__":
    unittest.main()
