import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.codex_auth import CodexAuthError, CodexAuthManager, prepare_auth_scope, read_auth_state
from termdeck.models import SessionRecord


def _record(codex_home: Path, cwd: Path) -> SessionRecord:
    return SessionRecord(
        id="td-auth",
        name="auth",
        tag="ai",
        command="bash",
        cwd=str(cwd),
        tmux_name="termdeck_auth",
        created_at="now",
        last_opened_at="now",
        auth_scope_id="auth-test",
        codex_home=str(codex_home),
    )


def _write_source(source: Path) -> None:
    accounts = source / "accounts"
    accounts.mkdir(parents=True)
    (accounts / "registry.json").write_text(
        json.dumps(
            {
                "active_account_key": "key-work",
                "previous_active_account_key": "key-personal",
                "accounts": [
                    {"account_key": "key-work", "email": "work@example.com", "alias": "work"},
                    {"account_key": "key-personal", "email": "me@example.com", "alias": "personal"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (accounts / "key-work.json").write_text('{"token":"work-secret"}', encoding="utf-8")
    (accounts / "key-personal.json").write_text('{"token":"personal-secret"}', encoding="utf-8")
    (source / "auth.json").write_text('{"token":"global-active"}', encoding="utf-8")


def _write_native_source(source: Path) -> None:
    accounts = source / "accounts"
    accounts.mkdir(parents=True)
    for name in ("alpha", "beta", "gamma"):
        (accounts / f"{name}.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "last_refresh": "2026-01-01T00:00:00Z",
                    "tokens": {"refresh_token": f"{name}-refresh"},
                }
            ),
            encoding="utf-8",
        )
    (source / "auth.json").write_text('{"source":"must-not-copy"}', encoding="utf-8")
    (source / "current").write_text("alpha\n", encoding="utf-8")


def _write_native_codex_auth(executable: Path) -> None:
    executable.write_text(
        r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path
home = Path.home() / '.codex'
if len(sys.argv) != 3 or sys.argv[1] != 'use':
    raise SystemExit(64)
name = sys.argv[2]
account = home / 'accounts' / f'{name}.json'
if not account.is_file():
    raise SystemExit(2)
auth = home / 'auth.json'
try:
    auth.unlink()
except FileNotFoundError:
    pass
auth.symlink_to(account.resolve())
(home / 'current').write_text(name + '\n', encoding='utf-8')
(home / 'last-command').write_text(' '.join(sys.argv[1:]) + '\n', encoding='utf-8')
if os.environ.get('FAKE_FAIL_ACCOUNT') == name:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)


class CodexAuthTests(unittest.TestCase):
    def test_prepare_scope_shares_canonical_sessions_without_sharing_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            rollout = source / "sessions" / "2026" / "07" / "26" / "rollout-test.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            archived = source / "archived_sessions"
            archived.mkdir()

            prepare_auth_scope(private, account_source=source)

            self.assertTrue((private / "sessions").is_symlink())
            self.assertEqual((private / "sessions").resolve(), (source / "sessions").resolve())
            self.assertEqual(
                (private / "sessions" / "2026" / "07" / "26" / "rollout-test.jsonl").read_text(encoding="utf-8"),
                '{"type":"session_meta"}\n',
            )
            self.assertTrue((private / "archived_sessions").is_symlink())
            self.assertEqual((private / "archived_sessions").resolve(), archived.resolve())
            self.assertFalse((private / "auth.json").exists())

    def test_prepare_scope_replaces_empty_private_sessions_with_shared_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            (source / "sessions").mkdir()
            (private / "sessions").mkdir(parents=True)

            prepare_auth_scope(private, account_source=source)

            self.assertTrue((private / "sessions").is_symlink())
            self.assertEqual((private / "sessions").resolve(), (source / "sessions").resolve())

    def test_prepare_scope_preserves_nonempty_legacy_private_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            (source / "sessions").mkdir()
            local_rollout = private / "sessions" / "2026" / "08" / "09" / "rollout-local.jsonl"
            local_rollout.parent.mkdir(parents=True)
            local_rollout.write_text('{"local":"must-survive"}\n', encoding="utf-8")

            prepare_auth_scope(private, account_source=source)

            self.assertTrue((private / "sessions").is_dir())
            self.assertFalse((private / "sessions").is_symlink())
            self.assertEqual(local_rollout.read_text(encoding="utf-8"), '{"local":"must-survive"}\n')
            self.assertEqual(list((source / "sessions").rglob("*.jsonl")), [])

    def test_prepare_scope_never_self_links_canonical_session_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_source(home)
            rollout = home / "sessions" / "2026" / "08" / "09" / "rollout-source.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"source":"keep"}\n', encoding="utf-8")

            prepare_auth_scope(home, account_source=home)

            self.assertTrue((home / "sessions").is_dir())
            self.assertFalse((home / "sessions").is_symlink())
            self.assertEqual(rollout.read_text(encoding="utf-8"), '{"source":"keep"}\n')

    def test_prepare_scope_rejects_unexpected_session_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            unexpected = root / "unexpected"
            _write_source(source)
            (source / "sessions").mkdir()
            unexpected.mkdir()
            private.mkdir()
            (private / "sessions").symlink_to(unexpected, target_is_directory=True)

            with self.assertRaises(CodexAuthError):
                prepare_auth_scope(private, account_source=source)

            self.assertTrue((private / "sessions").is_symlink())
            self.assertEqual((private / "sessions").resolve(), unexpected.resolve())

    def test_prepare_scope_copies_saved_accounts_but_starts_anonymous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)

            prepare_auth_scope(private, account_source=source)

            self.assertFalse((private / "auth.json").exists())
            self.assertTrue((private / "accounts" / "key-work.json").is_file())
            registry = json.loads((private / "accounts" / "registry.json").read_text(encoding="utf-8"))
            self.assertIsNone(registry["active_account_key"])
            self.assertIsNone(registry["previous_active_account_key"])
            self.assertEqual(json.loads((source / "accounts" / "registry.json").read_text())["active_account_key"], "key-work")
            self.assertEqual((source / "auth.json").read_text(), '{"token":"global-active"}')
            self.assertEqual((private / ".termdeck-compat-home" / ".codex").resolve(), private.resolve())
            self.assertTrue(os.access(private / "bin" / "codex-auth", os.X_OK))
            state = read_auth_state(private)
            self.assertIsNone(state.selected_account)
            self.assertEqual([account.label for account in state.accounts], ["work", "personal"])

    def test_prepare_scope_finds_user_npm_codex_auth_when_desktop_path_is_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_home = root / "host-home"
            source = root / "source"
            private = root / "private"
            executable = host_home / ".npm-global" / "bin" / "codex-auth"
            executable.parent.mkdir(parents=True)
            _write_native_codex_auth(executable)
            _write_native_source(source)

            with mock.patch("termdeck.codex_auth.Path.home", return_value=host_home), mock.patch.dict(
                os.environ,
                {"PATH": "/usr/local/bin:/usr/bin"},
                clear=False,
            ):
                prepare_auth_scope(private, account_source=source)
                state = read_auth_state(private)
                result = CodexAuthManager(account_source=source).switch(
                    _record(private, root),
                    "beta",
                )

            wrapper = (private / "bin" / "codex-auth").read_text(encoding="utf-8")
            self.assertTrue(state.codex_auth_available)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.state.selected_account, "beta")
            self.assertEqual((private / "auth.json").resolve(), (private / "accounts" / "beta.json").resolve())
            self.assertIn(f"exec {executable.resolve()} \"$@\"", wrapper)
            self.assertIn("export PATH=/usr/local/bin:/usr/bin", wrapper)

    def test_switch_changes_only_the_terminal_private_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_source(source)
            executable = fake_bin / "codex-auth"
            executable.write_text(
                """#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path
home = Path.home() / '.codex'
registry_path = home / 'accounts' / 'registry.json'
payload = json.loads(registry_path.read_text(encoding='utf-8'))
query = sys.argv[2].casefold()
account = next(row for row in payload['accounts'] if query in {row['account_key'].casefold(), row['email'].casefold(), row['alias'].casefold()})
payload['active_account_key'] = account['account_key']
registry_path.write_text(json.dumps(payload), encoding='utf-8')
shutil.copyfile(home / 'accounts' / f"{account['account_key']}.json", home / 'auth.json')
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"
            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                result = CodexAuthManager(account_source=source).switch(
                    _record(private, root),
                    "personal",
                )

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.state.selected_account, "personal")
            self.assertEqual(result.state.selected_account_key, "key-personal")
            private_auth = json.loads((private / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(private_auth["token"], "personal-secret")
            global_registry = json.loads((source / "accounts" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(global_registry["active_account_key"], "key-work")
            self.assertEqual((source / "auth.json").read_text(), '{"token":"global-active"}')


    def test_native_01x_layout_uses_use_current_and_private_auth_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)

            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"
            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                initial = read_auth_state(private)
                result = CodexAuthManager(account_source=source).switch(
                    _record(private, root),
                    "beta",
                )

            self.assertIsNone(initial.selected_account)
            self.assertEqual([account.label for account in initial.accounts], ["alpha", "beta", "gamma"])
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.state.selected_account, "beta")
            self.assertEqual((private / "current").read_text(encoding="utf-8").strip(), "beta")
            self.assertEqual((private / "last-command").read_text(encoding="utf-8").strip(), "use beta")
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual((private / "auth.json").resolve(), (private / "accounts" / "beta.json").resolve())
            self.assertEqual((source / "current").read_text(encoding="utf-8").strip(), "alpha")
            self.assertEqual((source / "auth.json").read_text(encoding="utf-8"), '{"source":"must-not-copy"}')

            (private / "current").unlink()
            self.assertEqual(read_auth_state(private).selected_account, "beta")

    def test_native_01x_auth_symlink_wins_over_stale_current_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            prepare_auth_scope(private, account_source=source)

            (private / "current").write_text("alpha\n", encoding="utf-8")
            (private / "auth.json").symlink_to((private / "accounts" / "beta.json").resolve())

            state = read_auth_state(private)
            self.assertEqual(state.selected_account, "beta")
            self.assertEqual(state.selected_account_key, "beta")

    def test_native_01x_switches_two_tabs_without_cross_tab_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            first = root / "first"
            second = root / "second"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"

            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(first, account_source=source)
                prepare_auth_scope(second, account_source=source)
                manager = CodexAuthManager(account_source=source)
                first_result = manager.switch(_record(first, root), "alpha")
                second_result = manager.switch(_record(second, root), "beta")
                second_changed = manager.switch(_record(second, root), "gamma")

            self.assertTrue(first_result.ok, first_result.error)
            self.assertTrue(second_result.ok, second_result.error)
            self.assertTrue(second_changed.ok, second_changed.error)
            self.assertEqual(read_auth_state(first).selected_account, "alpha")
            self.assertEqual(read_auth_state(second).selected_account, "gamma")
            self.assertEqual((first / "auth.json").resolve(), (first / "accounts" / "alpha.json").resolve())
            self.assertEqual((second / "auth.json").resolve(), (second / "accounts" / "gamma.json").resolve())
            self.assertNotEqual((first / "accounts" / "alpha.json").resolve(), (second / "accounts" / "alpha.json").resolve())
            self.assertEqual((source / "current").read_text(encoding="utf-8").strip(), "alpha")

    def test_native_existing_scope_refreshes_newer_non_active_saved_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"

            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                manager = CodexAuthManager(account_source=source)
                selected = manager.switch(_record(private, root), "alpha")
                self.assertTrue(selected.ok, selected.error)

                for name in ("alpha", "beta"):
                    (source / "accounts" / f"{name}.json").write_text(
                        json.dumps(
                            {
                                "auth_mode": "chatgpt",
                                "last_refresh": "2026-02-01T00:00:00Z",
                                "tokens": {"refresh_token": f"{name}-new"},
                            }
                        ),
                        encoding="utf-8",
                    )
                prepare_auth_scope(private, account_source=source)

            alpha = json.loads((private / "accounts" / "alpha.json").read_text(encoding="utf-8"))
            beta = json.loads((private / "accounts" / "beta.json").read_text(encoding="utf-8"))
            self.assertEqual(alpha["tokens"]["refresh_token"], "alpha-refresh")
            self.assertEqual(beta["tokens"]["refresh_token"], "beta-new")
            self.assertEqual(read_auth_state(private).selected_account, "alpha")

    def test_native_01x_failed_use_rolls_back_previous_private_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"

            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                manager = CodexAuthManager(account_source=source)
                record = _record(private, root)
                first = manager.switch(record, "alpha")
                with mock.patch.dict(os.environ, {"FAKE_FAIL_ACCOUNT": "gamma"}):
                    failed = manager.switch(record, "gamma")

            self.assertTrue(first.ok, first.error)
            self.assertFalse(failed.ok)
            self.assertEqual(failed.state.selected_account, "alpha")
            self.assertEqual((private / "current").read_text(encoding="utf-8").strip(), "alpha")
            self.assertEqual((private / "auth.json").resolve(), (private / "accounts" / "alpha.json").resolve())


    def test_failed_switch_rolls_back_to_the_previous_private_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_source(source)
            executable = fake_bin / "codex-auth"
            executable.write_text(
                """#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path
home = Path.home() / '.codex'
registry_path = home / 'accounts' / 'registry.json'
payload = json.loads(registry_path.read_text(encoding='utf-8'))
query = sys.argv[2].casefold()
account = next(row for row in payload['accounts'] if query in {row['account_key'].casefold(), row['email'].casefold(), row['alias'].casefold()})
payload['active_account_key'] = account['account_key']
registry_path.write_text(json.dumps(payload), encoding='utf-8')
shutil.copyfile(home / 'accounts' / f"{account['account_key']}.json", home / 'auth.json')
if account['alias'] == 'personal':
    raise SystemExit(1)
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"
            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                manager = CodexAuthManager(account_source=source)
                record = _record(private, root)
                first = manager.switch(record, "work")
                failed = manager.switch(record, "personal")

            self.assertTrue(first.ok)
            self.assertFalse(failed.ok)
            self.assertEqual(failed.state.selected_account, "work")
            self.assertEqual(failed.state.selected_account_key, "key-work")
            self.assertEqual(json.loads((private / "auth.json").read_text())["token"], "work-secret")

    def test_nested_termdeck_path_cannot_redirect_child_switch_to_parent_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            parent = root / "parent"
            child = root / "child"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_source(source)
            executable = fake_bin / "codex-auth"
            executable.write_text(
                """#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path
home = Path.home() / '.codex'
registry_path = home / 'accounts' / 'registry.json'
payload = json.loads(registry_path.read_text(encoding='utf-8'))
query = sys.argv[2].casefold()
account = next(row for row in payload['accounts'] if query in {row['account_key'].casefold(), row['email'].casefold(), row['alias'].casefold()})
payload['active_account_key'] = account['account_key']
registry_path.write_text(json.dumps(payload), encoding='utf-8')
shutil.copyfile(home / 'accounts' / f"{account['account_key']}.json", home / 'auth.json')
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            original_path = os.environ.get("PATH", "")
            direct_path = f"{fake_bin}:{original_path}"
            with mock.patch.dict(os.environ, {"PATH": direct_path}):
                prepare_auth_scope(parent, account_source=source)
                parent_result = CodexAuthManager(account_source=source).switch(
                    _record(parent, root),
                    "work",
                )
            nested_path = f"{parent / 'bin'}:{fake_bin}:{original_path}"
            with mock.patch.dict(os.environ, {"PATH": nested_path}):
                prepare_auth_scope(child, account_source=source)
                child_result = CodexAuthManager(account_source=source).switch(
                    _record(child, root),
                    "personal",
                )

            self.assertTrue(parent_result.ok, parent_result.error)
            self.assertTrue(child_result.ok, child_result.error)
            self.assertEqual(read_auth_state(parent).selected_account, "work")
            self.assertEqual(read_auth_state(child).selected_account, "personal")
            child_wrapper = (child / "bin" / "codex-auth").read_text(encoding="utf-8")
            self.assertNotIn(str(parent / "bin"), child_wrapper)
            self.assertIn(str(executable.resolve()), child_wrapper)



if __name__ == "__main__":
    unittest.main()
