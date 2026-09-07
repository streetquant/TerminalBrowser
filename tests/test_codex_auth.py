import fcntl
import json
import os
import pty
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from termdeck.codex_auth import (
    CodexAuthError,
    CodexAuthManager,
    prepare_auth_scope,
    read_auth_state,
    refresh_auth_scope_wrappers,
)
from termdeck.models import SessionRecord


def _write_complete_state_db(
    path: Path,
    *,
    status: str = "complete",
    thread_id: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
    connection.executescript(
        """
        CREATE TABLE _sqlx_migrations (
            version BIGINT PRIMARY KEY,
            description TEXT NOT NULL,
            installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            success BOOLEAN NOT NULL,
            checksum BLOB NOT NULL,
            execution_time BIGINT NOT NULL
        );
        CREATE TABLE backfill_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            last_watermark TEXT,
            last_success_at INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE thread_sections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            cwd TEXT NOT NULL,
            thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL
        );
        CREATE TABLE thread_dynamic_tools (
            thread_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY(thread_id, position),
            FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
        );
        CREATE TABLE thread_spawn_edges (id TEXT PRIMARY KEY);
        CREATE TABLE remote_control_enrollments (id TEXT PRIMARY KEY);
        CREATE TABLE external_agent_config_imports (id TEXT PRIMARY KEY);
        CREATE INDEX idx_threads_cwd ON threads(cwd);
        """
    )
    connection.executemany(
        """
        INSERT INTO _sqlx_migrations(
            version, description, success, checksum, execution_time
        ) VALUES (?, ?, 1, ?, ?)
        """,
        [
            (version, f"migration {version}", bytes([version]) * 48, version * 10)
            for version in range(1, 47)
        ],
    )
    connection.execute(
        """
        INSERT INTO backfill_state(
            id, status, last_watermark, last_success_at, updated_at
        ) VALUES (1, ?, 'sessions/last.jsonl', 1234, 1234)
        """,
        (status,),
    )
    connection.execute(
        "INSERT INTO thread_sections(id, name) VALUES (?, 'Pinned')",
        ("01984de2-8f74-7c91-a3b2-5c5e937cf318",),
    )
    if thread_id is not None:
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd) VALUES (?, ?, '/workspace')",
            (thread_id, f"/canonical/{thread_id}.jsonl"),
        )
    connection.commit()
    connection.close()


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
    _write_complete_state_db(
        source / "state_5.sqlite",
        thread_id="019f0000-0000-7000-8000-000000000001",
    )


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
    _write_complete_state_db(source / "state_5.sqlite")


def _bind_native_source_default(source: Path, account: str = "alpha") -> None:
    auth = source / "auth.json"
    auth.unlink(missing_ok=True)
    auth.symlink_to(Path("accounts") / f"{account}.json")
    (source / "current").write_text(f"{account}\n", encoding="utf-8")


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


def _write_native_codex_auth_picker(executable: Path) -> None:
    executable.write_text(
        r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

home = Path.home() / '.codex'
args = sys.argv[1:]
if not args or args[0] != 'use' or len(args) > 2:
    raise SystemExit(64)
name = args[1] if len(args) == 2 else ''
if not name:
    print('alpha\nbeta\ngamma', flush=True)
    name = sys.stdin.readline().strip()
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
(home / 'picker-observation.json').write_text(
    json.dumps({'argv': args, 'stdin_isatty': sys.stdin.isatty()}),
    encoding='utf-8',
)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)


def _write_delayed_native_codex_auth(executable: Path) -> None:
    executable.write_text(
        f"""#!{Path(sys.executable).resolve()}
import os
import sys
import time
from pathlib import Path

home = Path.home() / '.codex'
if sys.argv[1:] != ['use', 'beta']:
    raise SystemExit(64)
(home / 'delayed-picker-pid').write_text(str(os.getpid()), encoding='utf-8')
time.sleep(1.0)
(home / 'current').write_text('beta\\n', encoding='utf-8')
(home / 'auth.json').symlink_to((home / 'accounts' / 'beta.json').resolve())
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)


def _private_wrapper_environment(
    private: Path,
    *,
    scope_id: str = "auth-test",
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TERMDECK_AUTH_SCOPE_ID": scope_id,
            "CODEX_TERMDECK_GATE_ROOT": str(
                private / ".termdeck-codex-lifetime"
            ),
        }
    )
    return environment


def _run_picker_in_pty(
    executable: Path,
    *args: str,
    selection: str,
    env: dict[str, str] | None = None,
) -> None:
    master_fd, slave_fd = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [str(executable), *args],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, f"{selection}\n".encode())
        try:
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
            raise
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)
    if return_code:
        raise AssertionError(f"picker exited with status {return_code}")


def _run_contended_picker_in_pty(
    executable: Path,
    *args: str,
    selection: str,
    env: dict[str, str] | None = None,
) -> int:
    master_fd, slave_fd = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            [str(executable), *args],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)
        slave_fd = -1
        try:
            os.write(master_fd, f"{selection}\n".encode())
        except OSError:
            # Lock contention can end the wrapper before the PTY write wins
            # the scheduling race. Either ordering must remain bounded.
            pass
        try:
            return process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
            raise
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


class CodexAuthTests(unittest.TestCase):
    def test_prepare_scope_shares_canonical_config_profiles_and_wraps_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            launcher = root / "host-bin" / "codex"
            _write_source(source)
            (source / "config.toml").write_text(
                'model = "gpt-5"\nsqlite_home = "/canonical/state"\n',
                encoding="utf-8",
            )
            (source / "ollama.config.toml").write_text(
                'model_provider = "ollama"\n', encoding="utf-8"
            )
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o700)

            with mock.patch.dict(
                os.environ, {"TERMDECK_CODEX_BINARY": str(launcher)}
            ), mock.patch(
                "termdeck.codex_config_scope._launcher_is_attested",
                side_effect=lambda _candidate, resolved, _home, _attestor: (
                    resolved == launcher.resolve()
                ),
            ):
                prepare_auth_scope(private, account_source=source)

            for name in ("config.toml", "ollama.config.toml"):
                self.assertTrue((private / name).is_symlink(), name)
                self.assertEqual((private / name).resolve(), (source / name).resolve())
            wrapper = private / "bin" / "codex"
            self.assertTrue(wrapper.is_file())
            self.assertTrue(os.access(wrapper, os.X_OK))
            wrapper_text = wrapper.read_text(encoding="utf-8")
            self.assertIn(str(launcher.resolve()), wrapper_text)
            self.assertIn(
                f"sqlite_home={json.dumps(str(private.resolve()))}", wrapper_text
            )

    def test_prepare_scope_keeps_an_absent_anonymous_source_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "absent-source"
            private = root / "private"

            prepared = prepare_auth_scope(private, account_source=source)

            self.assertEqual(prepared, private.resolve())
            self.assertTrue(private.is_dir())
            self.assertFalse((private / "config.toml").exists())
            self.assertFalse((private / "bin" / "codex").exists())
            self.assertTrue((private / "bin" / "codex-auth").is_file())

    def test_prepare_scope_atomically_refreshes_existing_auth_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth_picker(executable)
            cache_home = root / "cache"
            generation_env = {
                "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                "XDG_CACHE_HOME": str(cache_home),
            }

            with mock.patch.dict(os.environ, generation_env):
                prepare_auth_scope(private, account_source=source)

            runner = private / "bin" / ".termdeck-codex-auth-lock-runner"
            wrapper = private / "bin" / "codex-auth"
            stale_inodes: dict[Path, int] = {}
            for path in (runner, wrapper):
                path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                path.chmod(0o700)
                stale_inodes[path] = path.stat().st_ino

            with mock.patch.dict(os.environ, generation_env):
                prepare_auth_scope(private, account_source=source)

            runner_text = runner.read_text(encoding="utf-8")
            wrapper_text = wrapper.read_text(encoding="utf-8")
            for path in (runner, wrapper):
                self.assertNotEqual(path.stat().st_ino, stale_inodes[path])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertIn(str(cache_home / "codex-contained-startup"), runner_text)
            self.assertIn("legacy Codex lifetime lock", runner_text)
            self.assertIn("canonical Codex lifetime lock", runner_text)
            self.assertIn("CODEX_TERMDECK_GATE_ROOT", wrapper_text)

    def test_wrapper_only_refresh_preserves_credentials_selection_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth_picker(executable)
            cache_home = root / "cache"
            generation_env = {
                "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                "XDG_CACHE_HOME": str(cache_home),
            }

            with mock.patch.dict(os.environ, generation_env):
                prepare_auth_scope(private, account_source=source)
            manager = CodexAuthManager(account_source=source)
            result = manager.switch(_record(private, root), "beta")
            self.assertTrue(result.ok, result.error)
            # This fixture has no shared rollout roots, so normal scope
            # preparation correctly has no reason to initialize a private DB.
            # Seed an opaque sentinel to prove the wrapper-only API never opens
            # or rewrites an existing state file.
            (private / "state_5.sqlite").write_bytes(b"private-state-sentinel")

            protected_paths = (
                source / "accounts" / "alpha.json",
                source / "accounts" / "beta.json",
                source / "accounts" / "gamma.json",
                private / "current",
                private / "state_5.sqlite",
            )
            before = {
                path: (path.lstat().st_ino, path.read_bytes())
                for path in protected_paths
            }
            auth_before = (
                (private / "auth.json").lstat().st_ino,
                os.readlink(private / "auth.json"),
            )
            (private / "bin" / ".termdeck-codex-auth-lock-runner").write_text(
                "#!/bin/sh\nexit 99\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, generation_env):
                runner, wrapper = refresh_auth_scope_wrappers(private)

            self.assertTrue(runner.is_file())
            self.assertTrue(wrapper.is_file())
            self.assertEqual(
                {
                    path: (path.lstat().st_ino, path.read_bytes())
                    for path in protected_paths
                },
                before,
            )
            self.assertEqual(
                (
                    (private / "auth.json").lstat().st_ino,
                    os.readlink(private / "auth.json"),
                ),
                auth_before,
            )

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
            source_state = sqlite3.connect(source / "state_5.sqlite")
            source_state.execute(
                "INSERT INTO thread_sections(id, name) VALUES ('custom-section', 'User section')"
            )
            source_state.commit()
            source_state.close()

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

            source_db = sqlite3.connect(source / "state_5.sqlite")
            private_db = sqlite3.connect(private / "state_5.sqlite")
            try:
                source_migrations = source_db.execute(
                    "SELECT version, description, success, checksum FROM _sqlx_migrations ORDER BY version"
                ).fetchall()
                private_migrations = private_db.execute(
                    "SELECT version, description, success, checksum FROM _sqlx_migrations ORDER BY version"
                ).fetchall()
                private_backfill = private_db.execute(
                    "SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state WHERE id=1"
                ).fetchone()
                private_threads = private_db.execute("SELECT id FROM threads").fetchall()
                private_sections = private_db.execute(
                    "SELECT id, name FROM thread_sections ORDER BY id"
                ).fetchall()
                private_auto_vacuum = private_db.execute("PRAGMA auto_vacuum").fetchone()[0]
            finally:
                source_db.close()
                private_db.close()

            self.assertEqual(private_migrations, source_migrations)
            self.assertEqual(private_backfill[0], "complete")
            self.assertIsNone(private_backfill[1])
            self.assertGreater(private_backfill[2], 0)
            self.assertEqual(private_backfill[3], private_backfill[2])
            self.assertEqual(private_threads, [])
            self.assertEqual(private_auto_vacuum, 2)
            self.assertEqual(
                private_sections,
                [("01984de2-8f74-7c91-a3b2-5c5e937cf318", "Pinned")],
            )
            self.assertFalse((private / "state_5.sqlite").is_symlink())
            self.assertNotEqual(
                (private / "state_5.sqlite").stat().st_ino,
                (source / "state_5.sqlite").stat().st_ino,
            )

    def test_prepare_scope_never_changes_an_existing_running_private_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            rollout = source / "sessions" / "2026" / "08" / "12" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            private.mkdir()
            _write_complete_state_db(
                private / "state_5.sqlite",
                status="running",
                thread_id="019f0000-0000-7000-8000-000000000099",
            )
            before = (private / "state_5.sqlite").read_bytes()

            prepare_auth_scope(private, account_source=source)

            self.assertEqual((private / "state_5.sqlite").read_bytes(), before)
            state = sqlite3.connect(private / "state_5.sqlite")
            try:
                self.assertEqual(
                    state.execute("SELECT status FROM backfill_state WHERE id=1").fetchone(),
                    ("running",),
                )
                self.assertEqual(
                    state.execute("SELECT count(*) FROM threads").fetchone(),
                    (1,),
                )
            finally:
                state.close()
            self.assertFalse((private / "sessions").exists())

    def test_prepare_scope_rejects_a_shared_mutable_state_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            (source / "sessions").mkdir()
            private.mkdir()
            (private / "state_5.sqlite").symlink_to(source / "state_5.sqlite")

            with self.assertRaisesRegex(CodexAuthError, "cannot be shared by symlink"):
                prepare_auth_scope(private, account_source=source)

            self.assertTrue((private / "state_5.sqlite").is_symlink())
            self.assertEqual(
                (private / "state_5.sqlite").resolve(),
                (source / "state_5.sqlite").resolve(),
            )

    def test_prepare_scope_rejects_a_hard_link_to_canonical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            rollout = source / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir()
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            private.mkdir()
            os.link(source / "state_5.sqlite", private / "state_5.sqlite")
            source_before = (source / "state_5.sqlite").read_bytes()

            with self.assertRaisesRegex(CodexAuthError, "must not be hard-linked"):
                prepare_auth_scope(private, account_source=source)

            self.assertEqual((source / "state_5.sqlite").read_bytes(), source_before)
            self.assertFalse((private / "sessions").exists())

    def test_prepare_scope_refuses_to_claim_an_active_source_backfill_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            (source / "state_5.sqlite").unlink()
            _write_complete_state_db(source / "state_5.sqlite", status="running")
            rollout = source / "sessions" / "2026" / "08" / "12" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")

            with self.assertRaisesRegex(CodexAuthError, "complete canonical Codex state database"):
                prepare_auth_scope(private, account_source=source)

            self.assertFalse((private / "state_5.sqlite").exists())
            self.assertFalse((private / "sessions").exists())

    def test_prepare_scope_rejects_non_native_complete_marker_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            state = sqlite3.connect(source / "state_5.sqlite")
            state.execute("UPDATE backfill_state SET status='COMPLETE' WHERE id=1")
            state.commit()
            state.close()
            rollout = source / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir()
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")

            with self.assertRaisesRegex(CodexAuthError, "complete canonical Codex state database"):
                prepare_auth_scope(private, account_source=source)

            self.assertFalse((private / "state_5.sqlite").exists())
            self.assertFalse((private / "sessions").exists())

    def test_prepare_scope_rejects_unsafe_sqlx_migration_ledgers(self):
        mutations = {
            "gap": "DELETE FROM _sqlx_migrations WHERE version=1",
            "text success": "UPDATE _sqlx_migrations SET success='false' WHERE version=1",
            "short checksum": "UPDATE _sqlx_migrations SET checksum=x'00' WHERE version=1",
            "unaudited future schema": (
                "INSERT INTO _sqlx_migrations(version, description, success, checksum, execution_time) "
                "VALUES (47, 'future', 1, zeroblob(48), 1)"
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                private = root / "private"
                _write_source(source)
                state = sqlite3.connect(source / "state_5.sqlite")
                state.execute(mutation)
                state.commit()
                state.close()
                rollout = source / "sessions" / "rollout.jsonl"
                rollout.parent.mkdir()
                rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")

                with self.assertRaisesRegex(
                    CodexAuthError, "complete canonical Codex state database"
                ):
                    prepare_auth_scope(private, account_source=source)

                self.assertFalse((private / "state_5.sqlite").exists())
                self.assertFalse((private / "sessions").exists())

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

    def test_prepare_scope_shares_saved_accounts_but_starts_anonymous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)

            prepare_auth_scope(private, account_source=source)

            self.assertFalse((private / "auth.json").exists())
            self.assertTrue((private / "accounts" / "key-work.json").is_symlink())
            self.assertTrue(
                os.path.samefile(
                    private / "accounts" / "key-work.json",
                    source / "accounts" / "key-work.json",
                )
            )
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

    def test_new_registry_scope_inherits_only_a_proven_canonical_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            (source / "auth.json").write_bytes(
                (source / "accounts" / "key-work.json").read_bytes()
            )

            prepare_auth_scope(private, account_source=source)
            state = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )

            self.assertEqual(state.selected_account_key, "key-work")
            self.assertTrue(state.canonical_binding_ready)
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "key-work.json",
            )
            self.assertTrue((private / "accounts" / "key-work.json").is_symlink())

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
            self.assertIn(str(executable.resolve()), wrapper)
            lock_runner = private / "bin" / ".termdeck-codex-auth-lock-runner"
            self.assertTrue(os.access(lock_runner, os.X_OK))
            self.assertIn(str(Path(sys.executable).resolve()), wrapper)
            self.assertIn(
                ".termdeck-auth-transaction.lock",
                lock_runner.read_text(encoding="utf-8"),
            )
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
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "key-personal.json",
            )
            (source / "accounts" / "key-personal.json").write_text(
                '{"token":"personal-refreshed"}',
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads((private / "auth.json").read_text())["token"],
                "personal-refreshed",
            )
            global_registry = json.loads((source / "accounts" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(global_registry["active_account_key"], "key-work")
            self.assertEqual((source / "auth.json").read_text(), '{"token":"global-active"}')


    def test_native_01x_layout_uses_private_current_and_shared_auth_symlink(self):
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
            self.assertFalse((private / "last-command").exists())
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual((private / "auth.json").resolve(), (private / "accounts" / "beta.json").resolve())
            self.assertEqual((source / "current").read_text(encoding="utf-8").strip(), "alpha")
            self.assertEqual((source / "auth.json").read_text(encoding="utf-8"), '{"source":"must-not-copy"}')

            (private / "current").unlink()
            self.assertEqual(read_auth_state(private).selected_account, "beta")

    def test_new_native_scope_inherits_proven_canonical_default_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            _bind_native_source_default(source, "beta")

            prepare_auth_scope(private, account_source=source)
            state = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )

            self.assertTrue(state.canonical_binding_ready)
            self.assertEqual(state.selected_account_key, "beta")
            self.assertTrue((private / "accounts").is_symlink())
            self.assertEqual((private / "accounts").resolve(), source / "accounts")
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "beta.json",
            )
            self.assertEqual((private / "current").read_text(), "beta\n")
            self.assertFalse(
                any(path.is_file() and not path.is_symlink() for path in private.glob("*.json"))
            )

            restored = CodexAuthManager(account_source=source).restore(
                _record(private, root),
                None,
            )
            self.assertTrue(restored.ok, restored.error)
            prepare_auth_scope(private, account_source=source)
            reopened = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )
            self.assertIsNone(reopened.selected_account_key)
            self.assertFalse((private / "auth.json").exists())
            self.assertFalse((private / "current").exists())

    def test_new_native_scope_accepts_exact_regular_canonical_auth_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            (source / "auth.json").write_bytes(
                (source / "accounts" / "alpha.json").read_bytes()
            )

            prepare_auth_scope(private, account_source=source)
            state = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )

            self.assertEqual(state.selected_account_key, "alpha")
            self.assertTrue(state.canonical_binding_ready)
            self.assertTrue((private / "accounts").is_symlink())
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "alpha.json",
            )

    def test_new_native_scope_rejects_unproven_canonical_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            # current names alpha but auth.json does not equal or point to the
            # alpha saved credential, so the new scope must remain anonymous.

            prepare_auth_scope(private, account_source=source)
            state = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )

            self.assertIsNone(state.selected_account_key)
            self.assertFalse((private / "auth.json").exists())
            self.assertFalse((private / "current").exists())
            self.assertTrue((private / "accounts").is_symlink())

    def test_new_native_scope_rejects_auth_link_that_disagrees_with_canonical_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            (source / "auth.json").unlink()
            (source / "auth.json").symlink_to(Path("accounts") / "beta.json")
            self.assertEqual((source / "current").read_text(), "alpha\n")

            prepare_auth_scope(private, account_source=source)
            state = CodexAuthManager(account_source=source).state(
                _record(private, root)
            )

            self.assertIsNone(state.selected_account_key)
            self.assertFalse((private / "auth.json").exists())
            self.assertFalse((private / "current").exists())
            self.assertTrue((private / "accounts").is_symlink())

    def test_same_key_switch_repairs_legacy_catalog_and_removes_stale_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            stale_accounts = private / "accounts"
            stale_accounts.mkdir(parents=True)
            for name in ("alpha", "beta", "gamma"):
                (stale_accounts / f"{name}.json").write_bytes(
                    (source / "accounts" / f"{name}.json").read_bytes()
                )
            (private / "current").write_text("beta\n", encoding="utf-8")
            (private / "auth.json").write_bytes(
                (source / "accounts" / "beta.json").read_bytes()
            )
            manager = CodexAuthManager(account_source=source)
            record = _record(private, root)

            before = manager.state(record)
            result = manager.switch(record, "beta")

            self.assertEqual(before.selected_account_key, "beta")
            self.assertFalse(before.canonical_binding_ready)
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.state.canonical_binding_ready)
            self.assertTrue((private / "accounts").is_symlink())
            self.assertEqual((private / "accounts").resolve(), source / "accounts")
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "beta.json",
            )
            self.assertFalse((private / ".termdeck-accounts-migration").exists())

    def test_private_wrapper_preserves_interactive_use_picker_arguments_and_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth_picker(executable)
            cache_home = root / "cache"
            source_accounts = {
                path.name: path.read_bytes()
                for path in (source / "accounts").iterdir()
            }

            with mock.patch.dict(
                os.environ,
                {
                    "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                    "XDG_CACHE_HOME": str(cache_home),
                },
            ):
                prepare_auth_scope(private, account_source=source)

            wrapper = private / "bin" / "codex-auth"
            wrapper_before = wrapper.read_bytes()
            wrapper_env = _private_wrapper_environment(private)

            _run_picker_in_pty(
                wrapper,
                "use",
                selection="beta",
                env=wrapper_env,
            )
            observation = json.loads(
                (private / "picker-observation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(observation["argv"], ["use"])
            self.assertTrue(observation["stdin_isatty"])
            self.assertEqual(read_auth_state(private).selected_account, "beta")
            self.assertEqual(
                (private / "auth.json").resolve(),
                (private / "accounts" / "beta.json").resolve(),
            )

            _run_picker_in_pty(
                wrapper,
                "use",
                "",
                selection="gamma",
                env=wrapper_env,
            )
            observation = json.loads(
                (private / "picker-observation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(observation["argv"], ["use", ""])
            self.assertTrue(observation["stdin_isatty"])
            self.assertEqual(read_auth_state(private).selected_account, "gamma")
            self.assertEqual(
                (private / "auth.json").resolve(),
                (private / "accounts" / "gamma.json").resolve(),
            )
            self.assertEqual(wrapper.read_bytes(), wrapper_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (source / "accounts").iterdir()
                },
                source_accounts,
            )
            self.assertEqual((source / "current").read_text(encoding="utf-8"), "alpha\n")
            self.assertEqual(
                (source / "auth.json").read_text(encoding="utf-8"),
                '{"source":"must-not-copy"}',
            )

    def test_private_wrapper_rejects_contended_python_lock_without_pointer_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth_picker(executable)
            cache_home = root / "cache"

            with mock.patch.dict(
                os.environ,
                {
                    "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                    "XDG_CACHE_HOME": str(cache_home),
                },
            ):
                prepare_auth_scope(private, account_source=source)

            wrapper = private / "bin" / "codex-auth"
            wrapper_env = _private_wrapper_environment(private)
            self.assertFalse(os.path.lexists(private / "auth.json"))
            self.assertFalse(os.path.lexists(private / "current"))

            lock_path = private / ".termdeck-auth-transaction.lock"
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return_code = _run_contended_picker_in_pty(
                    wrapper,
                    "use",
                    "",
                    selection="beta",
                    env=wrapper_env,
                )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            self.assertEqual(return_code, 75)
            self.assertFalse(os.path.lexists(private / "auth.json"))
            self.assertFalse(os.path.lexists(private / "current"))
            self.assertFalse((private / "picker-observation.json").exists())

            _run_picker_in_pty(
                wrapper,
                "use",
                "",
                selection="beta",
                env=wrapper_env,
            )
            observation = json.loads(
                (private / "picker-observation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(observation["argv"], ["use", ""])
            self.assertTrue(observation["stdin_isatty"])
            self.assertEqual(read_auth_state(private).selected_account, "beta")

    def test_private_wrapper_rejects_legacy_or_canonical_lifetime_owner(self):
        for domain in ("legacy", "canonical"):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                private = root / "private"
                fake_bin = root / "bin"
                fake_bin.mkdir()
                _write_native_source(source)
                executable = fake_bin / "codex-auth"
                _write_native_codex_auth_picker(executable)
                cache_home = root / "cache"

                with mock.patch.dict(
                    os.environ,
                    {
                        "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                        "XDG_CACHE_HOME": str(cache_home),
                    },
                ):
                    prepare_auth_scope(private, account_source=source)

                scope_id = "auth test/one"
                scope_key = "auth_test_one"
                legacy_root = cache_home / "codex-contained-startup"
                canonical_root = private / ".termdeck-codex-lifetime"
                for gate_root in (legacy_root, canonical_root):
                    gate_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    gate_root.chmod(0o700)
                lock_path = (
                    legacy_root / f"{scope_key}.lock"
                    if domain == "legacy"
                    else canonical_root / f"scope.{scope_key}.lock"
                )
                lock_fd = os.open(
                    lock_path,
                    os.O_RDWR
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o600,
                )
                wrapper = private / "bin" / "codex-auth"
                wrapper_env = _private_wrapper_environment(
                    private,
                    scope_id=scope_id,
                )
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return_code = _run_contended_picker_in_pty(
                        wrapper,
                        "use",
                        "beta",
                        selection="beta",
                        env=wrapper_env,
                    )
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)

                self.assertEqual(return_code, 75)
                self.assertFalse(os.path.lexists(private / "auth.json"))
                self.assertFalse(os.path.lexists(private / "current"))
                self.assertFalse((private / "picker-observation.json").exists())

                _run_picker_in_pty(
                    wrapper,
                    "use",
                    "beta",
                    selection="beta",
                    env=wrapper_env,
                )
                observation = json.loads(
                    (private / "picker-observation.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(observation["argv"], ["use", "beta"])
                self.assertEqual(read_auth_state(private).selected_account, "beta")

    def test_private_wrapper_kills_native_mutator_before_releasing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_delayed_native_codex_auth(executable)
            cache_home = root / "cache"

            with mock.patch.dict(
                os.environ,
                {
                    "TERMDECK_CODEX_AUTH_BINARY": str(executable),
                    "XDG_CACHE_HOME": str(cache_home),
                },
            ):
                prepare_auth_scope(private, account_source=source)

            wrapper = private / "bin" / "codex-auth"
            process = subprocess.Popen(
                [str(wrapper), "use", "beta"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_private_wrapper_environment(private),
            )
            pid_path = private / "delayed-picker-pid"
            deadline = time.monotonic() + 30
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not pid_path.exists():
                process.kill()
                process.wait(timeout=30)
                self.fail("native codex-auth did not start")
            child_pid = int(pid_path.read_text(encoding="utf-8"))

            os.kill(process.pid, signal.SIGKILL)
            self.assertEqual(process.wait(timeout=30), -signal.SIGKILL)

            lock_path = private / ".termdeck-auth-transaction.lock"
            probe_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            acquired = False
            deadline = time.monotonic() + 30
            try:
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(0.01)
                        continue
                    acquired = True
                    break
            finally:
                if acquired:
                    fcntl.flock(probe_fd, fcntl.LOCK_UN)
                os.close(probe_fd)

            self.assertTrue(acquired, "native child retained the transaction lock")
            child_deadline = time.monotonic() + 30
            child_stopped = False
            while time.monotonic() < child_deadline:
                if not Path(f"/proc/{child_pid}").exists():
                    child_stopped = True
                    break
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                except (FileNotFoundError, IndexError, OSError):
                    child_stopped = True
                    break
                if state == "Z":
                    child_stopped = True
                    break
                time.sleep(0.01)
            self.assertTrue(
                child_stopped,
                "native codex-auth survived its supervising runner",
            )

            time.sleep(1.1)
            self.assertFalse(os.path.lexists(private / "auth.json"))
            self.assertFalse(os.path.lexists(private / "current"))

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

    def test_selected_pointer_validation_rejects_marker_link_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            prepare_auth_scope(private, account_source=source)
            registry_path = private / "accounts" / "registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["active_account_key"] = "key-work"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            (private / "auth.json").symlink_to(
                Path("accounts") / "key-personal.json"
            )
            before_registry = registry_path.read_bytes()

            manager = CodexAuthManager(account_source=source)
            before = manager.state(_record(private, root))
            result = manager.switch(_record(private, root), "personal")

            self.assertEqual(before.selected_account_key, "key-personal")
            self.assertFalse(before.canonical_binding_ready)
            self.assertFalse(result.ok)
            self.assertIn(
                "auth link and selected-account marker identify different",
                result.error,
            )
            self.assertEqual(registry_path.read_bytes(), before_registry)
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "key-personal.json",
            )

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
            self.assertTrue((first / "accounts").is_symlink())
            self.assertTrue((second / "accounts").is_symlink())
            self.assertEqual((first / "accounts").resolve(), (source / "accounts").resolve())
            self.assertEqual((second / "accounts").resolve(), (source / "accounts").resolve())
            self.assertEqual((source / "current").read_text(encoding="utf-8").strip(), "alpha")

    def test_native_existing_scope_immediately_observes_source_refresh(self):
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
            self.assertEqual(alpha["tokens"]["refresh_token"], "alpha-new")
            self.assertEqual(beta["tokens"]["refresh_token"], "beta-new")
            self.assertEqual(read_auth_state(private).selected_account, "alpha")

    def test_native_polling_preserves_stale_private_copies_until_explicit_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            stale_accounts = private / "accounts"
            stale_accounts.mkdir(parents=True)
            for name in ("alpha", "beta", "gamma"):
                (stale_accounts / f"{name}.json").write_text(
                    json.dumps({"snapshot": f"stale-{name}"}),
                    encoding="utf-8",
                )
            (private / "current").write_text("beta\n", encoding="utf-8")
            (private / "auth.json").write_text('{"snapshot":"stale-beta"}', encoding="utf-8")

            manager = CodexAuthManager(account_source=source)
            record = _record(private, root)
            before = {
                path.relative_to(private): path.read_bytes()
                for path in private.rglob("*")
                if path.is_file()
            }
            polled = manager.state(record)

            self.assertFalse(polled.catalog_shared)
            self.assertFalse(polled.canonical_binding_ready)
            self.assertEqual(
                {
                    path.relative_to(private): path.read_bytes()
                    for path in private.rglob("*")
                    if path.is_file()
                },
                before,
            )
            self.assertFalse((private / "accounts").is_symlink())
            self.assertFalse((private / "auth.json").is_symlink())
            result = manager.switch(record, "beta")

            self.assertFalse(result.ok)
            self.assertIn("does not match exactly one", result.error)
            self.assertFalse((private / "accounts").is_symlink())
            (private / "auth.json").write_bytes(
                (source / "accounts" / "beta.json").read_bytes()
            )
            result = manager.switch(record, "beta")

            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.state.canonical_binding_ready)
            self.assertTrue((private / "accounts").is_symlink())
            self.assertEqual((private / "accounts").resolve(), (source / "accounts").resolve())
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual((private / "auth.json").resolve(), source / "accounts" / "beta.json")
            self.assertEqual(result.state.selected_account_key, "beta")
            self.assertFalse((private / ".termdeck-accounts-migration").exists())

    def test_native_migration_rejects_outside_auth_link_before_staging_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            outside = root / "outside.json"
            _write_native_source(source)
            accounts = private / "accounts"
            accounts.mkdir(parents=True)
            for name in ("alpha", "beta", "gamma"):
                (accounts / f"{name}.json").write_bytes(
                    (source / "accounts" / f"{name}.json").read_bytes()
                )
            outside.write_text('{"token":"outside"}', encoding="utf-8")
            (private / "current").write_text("alpha\n", encoding="utf-8")
            (private / "auth.json").symlink_to(outside)

            result = CodexAuthManager(account_source=source).switch(
                _record(private, root),
                "alpha",
            )

            self.assertFalse(result.ok)
            self.assertIn("points outside", result.error)
            self.assertTrue(accounts.is_dir())
            self.assertFalse(accounts.is_symlink())
            self.assertFalse((private / ".termdeck-accounts-migration").exists())
            self.assertEqual((private / "auth.json").resolve(), outside)
            self.assertEqual((private / "current").read_text(), "alpha\n")

    def test_native_migration_rejects_unsafe_current_before_mutation(self):
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                private = root / "private"
                outside = root / "outside-current"
                _write_native_source(source)
                accounts = private / "accounts"
                accounts.mkdir(parents=True)
                for name in ("alpha", "beta", "gamma"):
                    (accounts / f"{name}.json").write_bytes(
                        (source / "accounts" / f"{name}.json").read_bytes()
                    )
                (private / "auth.json").write_bytes(
                    (source / "accounts" / "alpha.json").read_bytes()
                )
                outside.write_text("alpha\n", encoding="utf-8")
                if link_kind == "symlink":
                    (private / "current").symlink_to(outside)
                else:
                    os.link(outside, private / "current")
                outside_mode = outside.stat().st_mode

                result = CodexAuthManager(account_source=source).switch(
                    _record(private, root),
                    "alpha",
                )

                self.assertFalse(result.ok)
                self.assertIn("current marker is unsafe", result.error)
                self.assertTrue(accounts.is_dir())
                self.assertFalse(accounts.is_symlink())
                self.assertFalse((private / ".termdeck-accounts-migration").exists())
                self.assertEqual(outside.read_text(), "alpha\n")
                self.assertEqual(outside.stat().st_mode, outside_mode)

    def test_registry_migration_fails_closed_on_stale_credential_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_source(source)
            prepare_auth_scope(private, account_source=source)
            accounts = private / "accounts"
            stale = accounts / ".key-work.json.migration-interrupted"
            stale.write_text('{"token":"stale-copy"}', encoding="utf-8")
            before = stale.read_bytes()

            manager = CodexAuthManager(account_source=source)
            before_state = manager.state(_record(private, root))
            result = manager.switch(_record(private, root), "work")

            self.assertFalse(before_state.catalog_shared)
            self.assertFalse(before_state.canonical_binding_ready)
            self.assertFalse(result.ok)
            self.assertIn("unresolved stale snapshot", result.error)
            self.assertEqual(stale.read_bytes(), before)
            self.assertFalse((private / "auth.json").exists())

    def test_native_shared_catalog_fingerprint_changes_on_external_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            prepare_auth_scope(private, account_source=source)
            before = read_auth_state(private)

            source_account = source / "accounts" / "alpha.json"
            replacement = source / "accounts" / ".alpha.replacement"
            replacement.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "last_refresh": "2026-08-12T00:00:00Z",
                        "tokens": {"refresh_token": "externally-refreshed"},
                    }
                ),
                encoding="utf-8",
            )
            replacement.chmod(0o600)
            replacement.replace(source_account)

            after = read_auth_state(private)

            self.assertNotEqual(after.fingerprint, before.fingerprint)
            self.assertEqual(
                json.loads((private / "accounts" / "alpha.json").read_text())["tokens"][
                    "refresh_token"
                ],
                "externally-refreshed",
            )

    def test_native_catalog_rejects_a_link_outside_the_configured_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            outside = root / "outside"
            _write_native_source(source)
            private.mkdir()
            outside.mkdir()
            (private / "accounts").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(CodexAuthError, "outside the configured source"):
                prepare_auth_scope(private, account_source=source)

            self.assertEqual((private / "accounts").resolve(), outside.resolve())

    def test_native_catalog_rejects_a_symlinked_source_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            outside = root / "outside.json"
            _write_native_source(source)
            outside.write_text('{"outside":true}', encoding="utf-8")
            account = source / "accounts" / "beta.json"
            account.unlink()
            account.symlink_to(outside)

            with self.assertRaisesRegex(CodexAuthError, "not a direct regular file"):
                prepare_auth_scope(private, account_source=source)

            self.assertTrue(private.is_dir())
            self.assertFalse((private / "accounts").exists())

    def test_native_catalog_rejects_group_writable_source_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            _write_native_source(source)
            source.chmod(0o770)

            with self.assertRaisesRegex(CodexAuthError, "writable by another user"):
                prepare_auth_scope(private, account_source=source)

            self.assertTrue(private.is_dir())
            self.assertFalse((private / "accounts").exists())

    def test_native_switch_does_not_invoke_external_codex_auth(self):
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
                    changed = manager.switch(record, "gamma")

            self.assertTrue(first.ok, first.error)
            self.assertTrue(changed.ok, changed.error)
            self.assertEqual(changed.state.selected_account, "gamma")
            self.assertFalse((private / "last-command").exists())
            self.assertEqual((private / "current").read_text(encoding="utf-8").strip(), "gamma")
            self.assertEqual((private / "auth.json").resolve(), (source / "accounts" / "gamma.json").resolve())

    def test_restore_returns_private_native_scope_from_beta_to_alpha(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)
            source_before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"

            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                manager = CodexAuthManager(account_source=source)
                record = _record(private, root)
                selected = manager.switch(record, "alpha")
                changed = manager.switch(record, "beta")
                restored = manager.restore(record, selected.state.selected_account_key)

            self.assertTrue(selected.ok, selected.error)
            self.assertTrue(changed.ok, changed.error)
            self.assertTrue(restored.ok, restored.error)
            self.assertEqual(restored.state.selected_account_key, "alpha")
            self.assertEqual(
                (private / "auth.json").resolve(),
                (private / "accounts" / "alpha.json").resolve(),
            )
            self.assertEqual(
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
                source_before,
            )

    def test_restore_returns_private_native_scope_from_beta_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            private = root / "private"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            _write_native_source(source)
            executable = fake_bin / "codex-auth"
            _write_native_codex_auth(executable)
            source_before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            host_path = f"{fake_bin}:{os.environ.get('PATH', '')}"

            with mock.patch.dict(os.environ, {"PATH": host_path}):
                prepare_auth_scope(private, account_source=source)
                manager = CodexAuthManager(account_source=source)
                record = _record(private, root)
                previous_key = manager.state(record).selected_account_key
                changed = manager.switch(record, "beta")
                restored = manager.restore(record, previous_key)

            self.assertIsNone(previous_key)
            self.assertTrue(changed.ok, changed.error)
            self.assertTrue(restored.ok, restored.error)
            self.assertIsNone(restored.state.selected_account_key)
            self.assertFalse((private / "auth.json").exists())
            self.assertFalse((private / "current").exists())
            self.assertEqual(
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
                source_before,
            )

    def test_restore_reports_failure_without_claiming_previous_account(self):
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
                selected = manager.switch(record, "beta")
                with mock.patch(
                    "termdeck.codex_auth._select_private_account",
                    side_effect=CodexAuthError("restore failed"),
                ):
                    restored = manager.restore(record, "alpha")

            self.assertTrue(selected.ok, selected.error)
            self.assertFalse(restored.ok)
            self.assertEqual(restored.error, "restore failed")
            self.assertEqual(restored.state.selected_account_key, "beta")
            self.assertEqual(
                (private / "auth.json").resolve(),
                (private / "accounts" / "beta.json").resolve(),
            )


    def test_registry_switch_does_not_invoke_external_codex_auth(self):
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
            self.assertTrue(failed.ok, failed.error)
            self.assertEqual(failed.state.selected_account, "personal")
            self.assertEqual(failed.state.selected_account_key, "key-personal")
            self.assertTrue((private / "auth.json").is_symlink())
            self.assertEqual(
                (private / "auth.json").resolve(),
                source / "accounts" / "key-personal.json",
            )

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
