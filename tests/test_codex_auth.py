import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.codex_auth import CodexAuthManager, prepare_auth_scope, read_auth_state
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


class CodexAuthTests(unittest.TestCase):
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
