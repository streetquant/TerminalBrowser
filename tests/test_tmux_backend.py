import os
import fcntl
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from termdeck.checkpointing import CheckpointResult
from termdeck.codex_auth import prepare_auth_scope
from termdeck.codex_config_scope import CodexConfigScopeError
from termdeck.history_control import (
    BackendHistoryState,
    ChildInputProtocol,
    EdgeKnowledge,
    HistoryOwner,
    ScrollDisposition,
)
from termdeck.models import SessionRecord
from termdeck.resources import process_tree
from termdeck.session_recipe import record_recipe_fields
from termdeck.tmux_backend import (
    AuthTransactionBusy,
    TmuxBackend,
    TmuxError,
    _AuthLifetimeLease,
    _AuthPaneIdentity,
    _acquire_auth_lifetime_lease,
    _auth_legacy_lifetime_lock_path,
    _auth_lifetime_lock_path,
    auth_transaction_lock,
)


def _record(**kwargs) -> SessionRecord:
    base = dict(
        id="td-test",
        name="test",
        tag="dev",
        command="bash",
        cwd="/tmp",
        tmux_name="termdeck_dev_test",
        created_at="now",
        last_opened_at="now",
    )
    base.update(kwargs)
    return SessionRecord(**base)


def _auth_record(**kwargs) -> SessionRecord:
    return _record(
        auth_scope_id="auth-test",
        codex_home="/tmp/auth-test",
        **kwargs,
    )


def _auth_identity(
    *,
    command: str = "bash",
    cwd: str = "/tmp",
    session_created: str = "1700000000",
    pane_id: str = "%1",
    pane_pid: int = 100,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tmux"],
        returncode=0,
        stdout=(
            f"{session_created}\t{pane_id}\t{pane_pid}\t{command}\t{cwd}\t0\n"
        ),
        stderr="",
    )


def _auth_pane_identity(
    *,
    command: str = "bash",
    cwd: str = "/tmp",
    session_created: str = "1700000000",
    pane_id: str = "%1",
    pane_pid: int = 100,
) -> _AuthPaneIdentity:
    return _AuthPaneIdentity(
        session_created=session_created,
        pane_id=pane_id,
        pane_pid=pane_pid,
        current_command=command,
        current_path=cwd,
    )


@contextmanager
def _stable_auth_process(
    backend: TmuxBackend,
    *,
    environment_ready: bool,
):
    """Provide PID-reuse-safe process evidence without depending on host PIDs."""

    lease = mock.Mock(name="auth_lifetime_lease")
    with mock.patch.object(
        TmuxBackend,
        "_auth_process_identity",
        return_value=("S", 1),
    ), mock.patch.object(
        TmuxBackend,
        "_private_auth_process_environment_ready",
        return_value=environment_ready,
    ), mock.patch(
        "termdeck.tmux_backend._acquire_auth_lifetime_lease",
        return_value=lease,
    ), mock.patch(
        "termdeck.tmux_backend._auth_transaction_is_held",
        return_value=True,
    ):
        yield lease


@contextmanager
def _auth_input_gate(backend: TmuxBackend):
    """Model a verified tmux input gate without invoking a real server."""

    with mock.patch.object(
        backend,
        "_disable_auth_pane_input",
        return_value=False,
    ) as disable, mock.patch.object(
        backend,
        "_restore_auth_pane_input",
    ) as restore:
        yield disable, restore


@contextmanager
def _private_launch_receipts(backend: TmuxBackend):
    """Isolate private-state tests from tmux/config and gate side effects."""

    lease = mock.Mock(name="private_launch_lease", held=True)
    with mock.patch.object(
        backend,
        "_auth_session_exists",
        return_value=False,
    ) as exists, mock.patch(
        "termdeck.tmux_backend._acquire_auth_lifetime_lease",
        return_value=lease,
    ) as acquire, mock.patch(
        "termdeck.tmux_backend.migrate_quiescent_codex_configuration",
    ) as migrate:
        yield exists, acquire, migrate, lease


def _write_state_db(
    path: Path,
    *,
    status: str = "complete",
    threads: list[tuple[str, str, str]] | None = None,
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
        CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            cwd TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL
        );
        CREATE TABLE thread_dynamic_tools (
            thread_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            input_schema TEXT NOT NULL,
            defer_loading INTEGER NOT NULL,
            namespace TEXT,
            PRIMARY KEY(thread_id, position),
            FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
        );
        CREATE TABLE thread_spawn_edges (id TEXT PRIMARY KEY);
        CREATE TABLE remote_control_enrollments (id TEXT PRIMARY KEY);
        CREATE TABLE external_agent_config_imports (id TEXT PRIMARY KEY);
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
    connection.executemany(
        "INSERT INTO threads(id, rollout_path, cwd) VALUES (?, ?, ?)",
        threads or [],
    )
    connection.commit()
    connection.close()


class FakeCheckpointManager:
    def __init__(self, *, restart_command=None):
        self._restart_command = restart_command
        self.wrapped = []
        self.checkpointed = []
        self.cleared = []

    def wrap_launch_command(self, record, command):
        self.wrapped.append((record.id, command))
        return f"wrapped({command})"

    def restart_command(self, record):
        return self._restart_command

    def session_dir(self, record):
        return Path("/checkpoints") / record.id

    def checkpoint(self, record):
        self.checkpointed.append(record.id)
        return CheckpointResult(record.id, "checkpoint", True, True, "saved", 1)

    def clear(self, record):
        self.cleared.append(record.id)


class TmuxBackendTests(unittest.TestCase):
    def assert_supervised_command(
        self,
        command: str,
        payload: str,
        *,
        fallback_shell: str | None = None,
    ) -> None:
        self.assertIn(payload, command)
        self.assertIn("__termdeck_status=$?", command)
        self.assertIn("interactive shell ready", command)
        if fallback_shell is not None:
            self.assertIn(f"exec {fallback_shell}", command)

    def test_resize_propagation_binds_witness_to_persistent_private_scope(self):
        expected = object()
        with mock.patch(
            "termdeck.tmux_backend._propagate_terminal_resize",
            return_value=expected,
        ) as propagate:
            result = TmuxBackend.propagate_terminal_resize(
                1234,
                codex_home="/tmp/auth-test",
                auth_scope_id="auth-test",
            )

        self.assertIs(result, expected)
        call = propagate.call_args
        self.assertEqual(call.args, (1234,))
        private_tuple = call.kwargs["expected_private_tuple"]
        self.assertEqual(private_tuple.codex_home, "/tmp/auth-test")
        self.assertEqual(private_tuple.auth_scope_id, "auth-test")
        self.assertEqual(
            private_tuple.gate_root,
            "/tmp/auth-test/.termdeck-codex-lifetime",
        )

    def test_resize_propagation_without_complete_persistent_scope_is_noop(self):
        with mock.patch("termdeck.tmux_backend._propagate_terminal_resize") as propagate:
            result = TmuxBackend.propagate_terminal_resize(
                1234,
                codex_home="/tmp/auth-test",
                auth_scope_id="auth-other",
            )

        self.assertFalse(result.signalled)
        self.assertEqual(
            result.reason,
            "persistent private Codex identity is unavailable",
        )
        propagate.assert_not_called()

    def test_auth_session_exists_retries_one_timeout_with_command_budget(self):
        backend = TmuxBackend(query_timeout=0.35, command_timeout=2.0)
        record = _auth_record()
        timeout = subprocess.CompletedProcess(
            ["tmux", "has-session"],
            124,
            "",
            "tmux command timed out after 0.35s: has-session",
        )
        exists = subprocess.CompletedProcess(
            ["tmux", "has-session"],
            0,
            "",
            "",
        )

        with mock.patch.object(backend, "_run", side_effect=[timeout, exists]) as run:
            self.assertTrue(backend._auth_session_exists(record))

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], backend.query_timeout)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], backend.command_timeout)
        self.assertTrue(run.call_args_list[1].kwargs["bypass_query_backoff"])

    def test_auth_session_exists_repeated_timeout_remains_fail_closed(self):
        backend = TmuxBackend(query_timeout=0.35, command_timeout=2.0)
        record = _auth_record()
        timeout = subprocess.CompletedProcess(
            ["tmux", "has-session"],
            124,
            "",
            "tmux command timed out",
        )

        with mock.patch.object(backend, "_run", side_effect=[timeout, timeout]) as run:
            with self.assertRaises(TmuxError):
                backend._auth_session_exists(record)

        self.assertEqual(run.call_count, 2)

    def test_recovery_pending_relaunches_exact_claude_session_without_dmtcp(self):
        session_id = "d2a7ed1b-d6fd-45de-8342-5f10cfed3c07"
        with tempfile.TemporaryDirectory() as tmp:
            saved_cwd = Path(tmp) / "saved"
            recovery_cwd = Path(tmp) / "recovery"
            saved_cwd.mkdir()
            recovery_cwd.mkdir()
            record = _record(
                command="cc-go-ulw",
                cwd=str(saved_cwd),
                recovery_command=f"cc-go-ulw --resume {session_id}",
                recovery_cwd=str(recovery_cwd),
                status="recovery-pending",
            )
            checkpoints = FakeCheckpointManager()
            backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
            completed = subprocess.CompletedProcess(
                args=["tmux"], returncode=0, stdout="", stderr=""
            )

            with mock.patch.object(
                backend, "session_exists", return_value=False
            ), mock.patch.object(backend, "_run", return_value=completed) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertEqual(
                launch[:-1],
                [
                    "new-session",
                    "-d",
                    "-s",
                    record.tmux_name,
                    "-c",
                    str(recovery_cwd.resolve()),
                ],
            )
            self.assert_supervised_command(
                launch[-1],
                f"cc-go-ulw --resume {session_id}",
            )
            self.assertEqual(checkpoints.wrapped, [])

    def test_crash_restart_relaunches_exact_codex_session_then_returns_to_the_shell(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            shell_cwd = Path(tmp) / "shell"
            recovery_cwd = Path(tmp) / "repo"
            shell_cwd.mkdir()
            recovery_cwd.mkdir()
            record = _record(
                command="/bin/bash",
                cwd=str(shell_cwd),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(recovery_cwd),
                status="new",
            )
            checkpoints = FakeCheckpointManager()
            backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
            completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

            with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertEqual(
                launch[:-1],
                [
                    "new-session",
                    "-d",
                    "-s",
                    record.tmux_name,
                    "-c",
                    str(recovery_cwd.resolve()),
                ],
            )
            self.assert_supervised_command(
                launch[-1],
                f"codex --yolo resume {session_id}",
                fallback_shell="/bin/bash",
            )
            self.assertEqual(checkpoints.wrapped, [])

    def test_exact_recovery_hydrates_missing_thread_into_private_state_db(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source?#%-state.sqlite"
            private_home = root / "auth-one"
            private_home.mkdir()
            private_bin = private_home / "bin"
            private_bin.mkdir()
            private_codex = private_bin / "codex"
            private_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            private_codex.chmod(0o700)
            private_db = private_home / "state_5.sqlite"
            sessions = root / "sessions"
            sessions.mkdir()
            rollout = sessions / "canonical-rollout.jsonl"
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            (private_home / "sessions").symlink_to(sessions, target_is_directory=True)
            _write_state_db(
                source_db,
                threads=[(session_id, str(rollout), str(root))],
            )
            _write_state_db(
                private_db,
                threads=[
                    (
                        "019f0000-0000-7000-8000-000000000001",
                        str(root / "local.jsonl"),
                        str(root),
                    )
                ],
            )
            source = sqlite3.connect(source_db)
            source.execute(
                "INSERT INTO thread_sections(id, name) VALUES ('custom-section', 'Recovered')"
            )
            source.execute(
                "UPDATE threads SET thread_section_id='custom-section' WHERE id=?",
                (session_id,),
            )
            source.execute(
                """
                INSERT INTO thread_dynamic_tools(
                    thread_id, position, name, description, input_schema,
                    defer_loading, namespace
                ) VALUES (?, 0, 'lookup', 'Exact tool', '{"type":"object"}', 1, 'recovery')
                """,
                (session_id,),
            )
            source.commit()
            source.close()

            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run", return_value=completed
            ):
                backend.create_or_resume(record)

            private = sqlite3.connect(private_db)
            try:
                rows = dict(private.execute("SELECT id, rollout_path FROM threads"))
                section = private.execute(
                    "SELECT id, name FROM thread_sections WHERE id='custom-section'"
                ).fetchone()
                tool = private.execute(
                    """
                    SELECT position, name, description, input_schema,
                           defer_loading, namespace
                    FROM thread_dynamic_tools WHERE thread_id=?
                    """,
                    (session_id,),
                ).fetchone()
            finally:
                private.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[session_id], str(rollout))
            self.assertIn("019f0000-0000-7000-8000-000000000001", rows)
            self.assertEqual(section, ("custom-section", "Recovered"))
            self.assertEqual(
                tool,
                (0, "lookup", "Exact tool", '{"type":"object"}', 1, "recovery"),
            )

    def test_exact_recovery_fails_closed_when_thread_metadata_cannot_be_hydrated(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source-state.sqlite"
            private_home = root / "auth-one"
            private_home.mkdir()
            private_db = private_home / "state_5.sqlite"
            _write_state_db(source_db)
            _write_state_db(private_db)

            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")
            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "metadata is absent"):
                backend.create_or_resume(record)

            run.assert_not_called()

    def test_exact_recovery_rolls_back_on_dynamic_tool_schema_mismatch(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source-state.sqlite"
            private_home = root / "auth-one"
            private_home.mkdir()
            private_db = private_home / "state_5.sqlite"
            sessions = root / "sessions"
            sessions.mkdir()
            rollout = sessions / "canonical-rollout.jsonl"
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            (private_home / "sessions").symlink_to(sessions, target_is_directory=True)
            _write_state_db(
                source_db,
                threads=[(session_id, str(rollout), str(root))],
            )
            _write_state_db(private_db)
            private = sqlite3.connect(private_db)
            private.execute("ALTER TABLE thread_dynamic_tools ADD COLUMN corrupt TEXT")
            private.commit()
            private.close()
            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "dynamic_tools schema mismatch"):
                backend.create_or_resume(record)

            run.assert_not_called()
            private = sqlite3.connect(private_db)
            try:
                self.assertEqual(
                    private.execute("SELECT count(*) FROM threads WHERE id=?", (session_id,)).fetchone(),
                    (0,),
                )
            finally:
                private.close()

    def test_exact_recovery_rejects_rollout_outside_shared_history(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source-state.sqlite"
            private_home = root / "auth-one"
            private_home.mkdir()
            private_db = private_home / "state_5.sqlite"
            sessions = root / "sessions"
            sessions.mkdir()
            (private_home / "sessions").symlink_to(sessions, target_is_directory=True)
            outside = root / "outside.jsonl"
            outside.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            _write_state_db(
                source_db,
                threads=[(session_id, str(outside), str(root))],
            )
            _write_state_db(private_db)
            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "outside the private shared-history root"):
                backend.create_or_resume(record)

            run.assert_not_called()

    def test_exact_recovery_does_not_write_into_a_running_private_backfill(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source-state.sqlite"
            private_home = root / "auth-one"
            private_db = private_home / "state_5.sqlite"
            _write_state_db(
                source_db,
                threads=[(session_id, str(root / "canonical-rollout.jsonl"), str(root))],
            )
            _write_state_db(private_db, status="running")
            before = private_db.read_bytes()
            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "backfill is not complete"):
                backend.create_or_resume(record)

            run.assert_not_called()
            self.assertEqual(private_db.read_bytes(), before)

    def test_ordinary_codex_launch_does_not_start_on_incomplete_private_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_home = root / "auth-one"
            private_db = private_home / "state_5.sqlite"
            _write_state_db(private_db, status="pending")
            before = private_db.read_bytes()
            record = _record(
                command="codex",
                cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")

            with _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(
                TmuxError, "backfill is not complete"
            ):
                backend.create_or_resume(record)

            run.assert_not_called()
            self.assertEqual(private_db.read_bytes(), before)

    def test_exact_recovery_rejects_a_different_migration_identity(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source-state.sqlite"
            private_home = root / "auth-one"
            private_db = private_home / "state_5.sqlite"
            _write_state_db(
                source_db,
                threads=[(session_id, str(root / "canonical-rollout.jsonl"), str(root))],
            )
            _write_state_db(private_db)
            private = sqlite3.connect(private_db)
            private.execute(
                "UPDATE _sqlx_migrations SET checksum=? WHERE version=1",
                (b"d" * 48,),
            )
            private.commit()
            private.close()
            before = private_db.read_bytes()
            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": str(source_db)}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "metadata is absent"):
                backend.create_or_resume(record)

            run.assert_not_called()
            self.assertEqual(private_db.read_bytes(), before)

    def test_cold_shared_scope_starts_sparse_then_hydrates_only_exact_recovery(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        unrelated_id = "019f0000-0000-7000-8000-000000000099"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_home = root / "source"
            private_home = root / "auth-one"
            source_db = source_home / "state_5.sqlite"
            rollout = source_home / "sessions" / "2026" / "08" / "12" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            _write_state_db(
                source_db,
                threads=[
                    (session_id, str(rollout), str(root)),
                    (unrelated_id, str(root / "unrelated.jsonl"), str(root)),
                ],
            )
            # Exact recovery requires the private scope's executable wrapper.
            # Make that prerequisite deterministic instead of depending on a
            # host-level `codex` binary being discoverable in the test runner.
            host_codex = root / "host-codex"
            host_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            host_codex.chmod(0o700)
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_BINARY": str(host_codex)},
                clear=False,
            ):
                prepare_auth_scope(private_home, account_source=source_home)
            self.assertTrue((private_home / "bin" / "codex").is_file())
            self.assertTrue(os.access(private_home / "bin" / "codex", os.X_OK))
            private_db = private_home / "state_5.sqlite"
            state = sqlite3.connect(private_db)
            try:
                self.assertEqual(
                    state.execute("SELECT status FROM backfill_state WHERE id=1").fetchone(),
                    ("complete",),
                )
                self.assertEqual(state.execute("SELECT count(*) FROM threads").fetchone(), (0,))
            finally:
                state.close()

            record = _record(
                command="/bin/bash",
                cwd=str(root),
                recovery_command=f"codex --yolo resume {session_id}",
                recovery_cwd=str(root),
                status="new",
                auth_scope_id="auth-one",
                codex_home=str(private_home),
            )
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(
                args=["tmux"], returncode=0, stdout="", stderr=""
            )
            with mock.patch.dict(
                "os.environ", {"TERMDECK_CODEX_STATE_SOURCE": ""}, clear=False
            ), _private_launch_receipts(backend), mock.patch.object(
                backend, "_run", return_value=completed
            ):
                backend.create_or_resume(record)

            state = sqlite3.connect(private_db)
            try:
                self.assertEqual(
                    state.execute("SELECT id FROM threads ORDER BY id").fetchall(),
                    [(session_id,)],
                )
            finally:
                state.close()

    def test_missing_recovery_directory_falls_back_to_the_saved_tab_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "deleted-repo"
            record = _record(
                command="/bin/bash",
                cwd=tmp,
                recovery_command="codex --yolo",
                recovery_cwd=str(missing),
            )
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(
                args=["tmux"], returncode=0, stdout="", stderr=""
            )

            with mock.patch.object(
                backend, "session_exists", return_value=False
            ), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertEqual(launch[launch.index("-c") + 1], tmp)
            self.assertIn(
                "TERMDECK_RECOVERY_DEGRADED=missing-working-directory",
                launch,
            )
            self.assertIn(
                f"TERMDECK_RECOVERY_ORIGINAL_CWD={missing}",
                launch,
            )
            self.assertFalse(missing.exists())

    def test_opaque_recovery_metadata_is_replayed_as_user_owned_recipe(self):
        record = _record(
            command="/bin/bash",
            recovery_command="custom-runner --config api_key=secret",
            status="recovery-pending",
        )
        checkpoints = FakeCheckpointManager()
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        # Recovery commands are opaque user-owned launch recipes. Core recovery
        # neither requires nor applies a Codex/Claude parser to decide whether a
        # command may be restored.
        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            record.recovery_command,
            fallback_shell=record.command,
        )
        self.assertEqual(checkpoints.wrapped, [])

    def test_gui_sized_launch_sets_exact_first_tmux_grid(self):
        record = _record(command="codex --yolo", status="new")
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume_at_grid(record, columns=132, rows=43)

        launch = run.call_args_list[0].args[0]
        self.assertEqual(launch[:8], [
            "new-session",
            "-d",
            "-s",
            record.tmux_name,
            "-x",
            "132",
            "-y",
            "43",
        ])
        self.assert_supervised_command(launch[-1], "codex --yolo")

    def test_gui_sized_launch_rejects_invalid_grids_before_tmux(self):
        record = _record(command="/bin/bash", status="new")
        backend = TmuxBackend(executable="tmux")
        invalid = ((1, 24), (80, 1), (1001, 24), (80, 1001), (True, 24))
        for columns, rows in invalid:
            with self.subTest(columns=columns, rows=rows), mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "initial terminal grid"):
                backend.create_or_resume_at_grid(
                    record,
                    columns=columns,
                    rows=rows,
                )
            run.assert_not_called()

    def test_default_backend_launches_new_non_codex_tabs_without_dmtcp(self):
        record = _record(command="/bin/bash", status="new")
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.create_or_resume(record)

        self.assertEqual(run.call_args_list[0].args[0][-1], "/bin/bash")
        self.assertNotIn("dmtcp", run.call_args_list[0].args[0][-1].casefold())
        self.assertFalse(backend.checkpoints.available)

    def test_recovery_pending_absolute_shell_without_recovery_state_restarts_fresh(self):
        checkpoints = FakeCheckpointManager()
        record = _record(command="/bin/bash", status="recovery-pending")
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            "wrapped(/bin/bash)",
            fallback_shell="/bin/bash",
        )
        self.assertEqual(checkpoints.wrapped, [(record.id, "/bin/bash")])

    def test_recovery_pending_unqualified_or_argument_shell_replays_recipe(self):
        checkpoints = FakeCheckpointManager()
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        for command in ("bash", "/bin/bash -l"):
            record = _record(command=command, status="recovery-pending")
            with self.subTest(command=command), mock.patch.object(
                backend, "session_exists", return_value=False
            ), mock.patch.object(backend, "_run", return_value=completed) as run:
                backend.create_or_resume(record)
            self.assert_supervised_command(
                run.call_args_list[0].args[0][-1],
                f"wrapped({command})",
                fallback_shell=command,
            )
        self.assertEqual(
            checkpoints.wrapped,
            [("td-test", "bash"), ("td-test", "/bin/bash -l")],
        )

    def test_recovery_pending_arbitrary_absolute_executable_replays_recipe(self):
        checkpoints = FakeCheckpointManager()
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "custom-worker"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            record = _record(command=str(executable), status="recovery-pending")

            with mock.patch.object(
                backend, "session_exists", return_value=False
            ), mock.patch.object(backend, "_run", return_value=completed) as run:
                backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            f"wrapped({executable})",
        )
        self.assertEqual(checkpoints.wrapped, [(record.id, str(executable))])

    def test_recovery_pending_opaque_resume_metadata_replays_without_tool_parser(self):
        checkpoints = FakeCheckpointManager()
        record = _record(
            command="/bin/bash",
            recovery_command="arbitrary-tui --resume job-17",
            recovery_cwd="/tmp",
            status="recovery-pending",
        )
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        launch = run.call_args_list[0].args[0]
        self.assert_supervised_command(
            launch[-1],
            record.recovery_command,
            fallback_shell=record.command,
        )
        self.assertEqual(launch[launch.index("-c") + 1], "/tmp")
        self.assertEqual(checkpoints.wrapped, [])

    def test_missing_process_tree_restores_checkpoint_without_command_replay(self):
        checkpoints = FakeCheckpointManager(restart_command="restart-image")
        record = _record(command="python worker.py", status="recovery-pending")
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.create_or_resume(record)

        launch = run.call_args_list[0].args[0]
        self.assertEqual(launch[5], str(Path("/checkpoints") / record.id))
        self.assert_supervised_command(launch[-1], "restart-image")
        self.assertNotIn("python worker.py", launch[-1])

    def test_recovery_pending_without_native_state_recreates_saved_command(self):
        checkpoints = FakeCheckpointManager()
        record = _record(
            command="generic-agent --interactive",
            recovery_command="generic-agent --interactive",
            status="recovery-pending",
        )
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            record.recovery_command,
        )
        self.assertEqual(checkpoints.wrapped, [])

    def test_recovery_pending_bare_unknown_launcher_recreates_saved_command(self):
        checkpoints = FakeCheckpointManager()
        record = _record(
            command="unknown-tui-launcher",
            recovery_command="unknown-tui-launcher",
            status="recovery-pending",
        )
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            record.recovery_command,
        )
        self.assertEqual(checkpoints.wrapped, [])

    def test_reclassified_new_claude_launcher_may_start_fresh(self):
        checkpoints = FakeCheckpointManager()
        record = _record(command="cc-go-ulw", status="new")
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )

        with mock.patch.object(
            backend, "session_exists", return_value=False
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            "wrapped(cc-go-ulw)",
        )
        self.assertEqual(checkpoints.wrapped, [(record.id, "cc-go-ulw")])

    def test_deliberately_new_codex_tab_may_start_a_new_session(self):
        checkpoints = FakeCheckpointManager()
        record = _record(command="codex --yolo", status="new")
        backend = TmuxBackend(executable="tmux", checkpoint_manager=checkpoints)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

        with mock.patch.object(backend, "session_exists", return_value=False), mock.patch.object(
            backend,
            "_run",
            return_value=completed,
        ) as run:
            backend.create_or_resume(record)

        self.assert_supervised_command(
            run.call_args_list[0].args[0][-1],
            "codex --yolo",
        )
        self.assertEqual(checkpoints.wrapped, [])

    def test_private_codex_home_is_injected_into_new_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                command="/bin/bash",
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")

            with _private_launch_receipts(backend), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertIn(f"CODEX_HOME={codex_home.resolve()}", launch)
            self.assertIn("TERMDECK_AUTH_SCOPE_ID=auth-one", launch)
            path_entry = next(item for item in launch if item.startswith("PATH="))
            self.assertTrue(path_entry.split("=", 1)[1].startswith(str(codex_home.resolve() / "bin")))
            self.assertEqual(launch[-3:-1], ["-c", tmp])
            self.assertIn("bash --noprofile --rcfile", launch[-1])
            self.assertIn(str(codex_home / "bin" / ".termdeck-bashrc"), launch[-1])
            self.assertTrue(launch[-1].endswith(" -i"))

    def test_private_bash_recipe_uses_private_rcfile_across_equivalent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            encoded, digest, policy = record_recipe_fields(
                command="/bin/bash",
                cwd=tmp,
                restart_policy="on-failure",
            )
            record = _record(
                command="/usr/bin/bash",
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
                recovery_recipe=encoded,
                recovery_recipe_sha256=digest,
                restart_policy=policy,
            )
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(
                args=["tmux"], returncode=0, stdout="", stderr=""
            )

            with _private_launch_receipts(backend), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                backend.create_or_resume(record)

            launch = run.call_args_list[0].args[0]
            self.assertEqual(launch[-3:-1], ["-c", tmp])
            self.assertIn("bash --noprofile --rcfile", launch[-1])
            self.assertIn(str(codex_home / "bin" / ".termdeck-bashrc"), launch[-1])
            self.assertTrue(launch[-1].endswith(" -i"))
            self.assertIn(f"CODEX_HOME={codex_home.resolve()}", launch)
            self.assertIn("TERMDECK_AUTH_SCOPE_ID=auth-one", launch)

    def test_live_private_session_skips_config_migration_and_new_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.object(
                backend, "_auth_session_exists", return_value=True
            ) as exists, mock.patch.object(
                backend, "_pane_lifecycle", return_value=None
            ), mock.patch(
                "termdeck.tmux_backend._acquire_auth_lifetime_lease"
            ) as acquire, mock.patch(
                "termdeck.tmux_backend.migrate_quiescent_codex_configuration"
            ) as migrate, mock.patch(
                "termdeck.tmux_backend.ensure_private_codex_state"
            ) as ensure, mock.patch.object(backend, "_run") as run:
                backend.create_or_resume(record)

            exists.assert_called_once_with(record)
            acquire.assert_not_called()
            migrate.assert_not_called()
            ensure.assert_not_called()
            run.assert_not_called()

    def test_absent_private_session_migrates_config_before_state_and_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                command="/bin/bash",
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")
            events = []
            lease = mock.Mock(name="lifetime_lease", held=True)
            lease.release.side_effect = lambda: events.append("lease-release")
            completed = subprocess.CompletedProcess(
                args=["tmux"], returncode=0, stdout="", stderr=""
            )

            with mock.patch.object(
                backend, "_auth_session_exists", return_value=False
            ) as exists, mock.patch(
                "termdeck.tmux_backend._acquire_auth_lifetime_lease",
                return_value=lease,
            ), mock.patch(
                "termdeck.tmux_backend.migrate_quiescent_codex_configuration",
                side_effect=lambda _home: events.append("migrate"),
            ) as migrate, mock.patch(
                "termdeck.tmux_backend.ensure_private_codex_state",
                side_effect=lambda _home, **_kwargs: events.append("ensure"),
            ) as ensure, mock.patch.object(
                backend,
                "_run",
                side_effect=lambda args, **_kwargs: (
                    events.append(args[0]) or completed
                ),
            ) as run:
                backend.create_or_resume(record)

            self.assertEqual(exists.call_args_list, [mock.call(record), mock.call(record)])
            migrate.assert_called_once_with(str(codex_home))
            ensure.assert_called_once_with(str(codex_home), require_complete=True)
            self.assertEqual(run.call_args_list[0].args[0][0], "new-session")
            self.assertEqual(events[:3], ["migrate", "ensure", "new-session"])
            self.assertEqual(events[-1], "lease-release")
            lease.release.assert_called_once_with()

    def test_indeterminate_private_liveness_fails_before_config_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.object(
                backend,
                "_auth_session_exists",
                side_effect=TmuxError("tmux liveness is indeterminate"),
            ), mock.patch(
                "termdeck.tmux_backend._acquire_auth_lifetime_lease"
            ) as acquire, mock.patch(
                "termdeck.tmux_backend.migrate_quiescent_codex_configuration"
            ) as migrate, mock.patch(
                "termdeck.tmux_backend.ensure_private_codex_state"
            ) as ensure, mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(TmuxError, "liveness is indeterminate"):
                backend.create_or_resume(record)

            acquire.assert_not_called()
            migrate.assert_not_called()
            ensure.assert_not_called()
            run.assert_not_called()

    def test_config_migration_failure_prevents_private_state_and_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")
            lease = mock.Mock(name="lifetime_lease", held=True)

            with mock.patch.object(
                backend, "_auth_session_exists", return_value=False
            ) as exists, mock.patch(
                "termdeck.tmux_backend._acquire_auth_lifetime_lease",
                return_value=lease,
            ), mock.patch(
                "termdeck.tmux_backend.migrate_quiescent_codex_configuration",
                side_effect=CodexConfigScopeError("legacy config rollback complete"),
            ) as migrate, mock.patch(
                "termdeck.tmux_backend.ensure_private_codex_state"
            ) as ensure, mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(
                TmuxError, "could not migrate stopped private Codex configuration"
            ):
                backend.create_or_resume(record)

            self.assertEqual(exists.call_args_list, [mock.call(record), mock.call(record)])
            migrate.assert_called_once_with(str(codex_home))
            ensure.assert_not_called()
            run.assert_not_called()
            lease.release.assert_called_once_with()

    def test_prelaunch_migration_failure_releases_both_real_lifetime_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            codex_home = root / "auth-one"
            codex_home.mkdir()
            record = _record(
                cwd=tmp,
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")

            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
            ), mock.patch.object(
                backend, "_auth_session_exists", return_value=False
            ), mock.patch(
                "termdeck.tmux_backend.migrate_quiescent_codex_configuration",
                side_effect=CodexConfigScopeError("rollback complete"),
            ), mock.patch(
                "termdeck.tmux_backend.ensure_private_codex_state"
            ) as ensure, mock.patch.object(
                backend, "_run"
            ) as run, self.assertRaisesRegex(
                TmuxError, "could not migrate stopped private Codex configuration"
            ):
                backend.create_or_resume(record)

            ensure.assert_not_called()
            run.assert_not_called()
            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
            ):
                for path in (
                    _auth_legacy_lifetime_lock_path(record),
                    _auth_lifetime_lock_path(record),
                ):
                    fd = os.open(path, os.O_RDWR | os.O_APPEND)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)

    def test_private_scope_identity_is_rejected_before_lock_or_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            backend = TmuxBackend(executable="tmux")
            invalid_records = (
                _record(auth_scope_id="auth-one!", codex_home=str(codex_home)),
                _record(auth_scope_id="auth-two", codex_home=str(codex_home)),
            )

            for record in invalid_records:
                with self.subTest(scope=record.auth_scope_id), mock.patch.object(
                    backend, "_auth_session_exists"
                ) as exists, mock.patch(
                    "termdeck.tmux_backend._acquire_auth_lifetime_lease"
                ) as acquire, mock.patch.object(backend, "_run") as run, self.assertRaisesRegex(
                    TmuxError, "must match its CODEX_HOME basename"
                ):
                    backend.create_or_resume(record)

                exists.assert_not_called()
                acquire.assert_not_called()
                run.assert_not_called()
                self.assertFalse((codex_home / ".termdeck-auth-transaction.lock").exists())

    def test_relative_private_codex_home_is_rejected_before_lock_or_spawn(self):
        record = _record(auth_scope_id="auth-one", codex_home="relative/auth-one")
        backend = TmuxBackend(executable="tmux")

        with mock.patch.object(
            backend, "_auth_session_exists"
        ) as exists, mock.patch(
            "termdeck.tmux_backend._acquire_auth_lifetime_lease"
        ) as acquire, mock.patch.object(backend, "_run") as run, self.assertRaisesRegex(
            TmuxError, "CODEX_HOME must be absolute"
        ):
            backend.create_or_resume(record)

        exists.assert_not_called()
        acquire.assert_not_called()
        run.assert_not_called()

    def test_relative_xdg_cache_home_fails_before_lifetime_lock_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(auth_scope_id="auth-one", codex_home=str(codex_home))

            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": "relative-cache"}, clear=False
            ), self.assertRaisesRegex(TmuxError, "XDG_CACHE_HOME must be absolute"):
                _acquire_auth_lifetime_lease(record)

            self.assertFalse((codex_home / ".termdeck-codex-lifetime").exists())

    def test_dual_lifetime_lease_acquires_legacy_then_canonical_and_releases_reverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            codex_home = root / "auth-one"
            codex_home.mkdir()
            record = _record(auth_scope_id="auth-one", codex_home=str(codex_home))
            events = []
            real_open = os.open
            real_close = os.close

            def open_lock(path, flags, mode=0o777):
                events.append(("open", Path(path)))
                return real_open(path, flags, mode)

            def close_lock(fd):
                events.append(("close", fd))
                real_close(fd)

            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
            ), mock.patch("termdeck.tmux_backend.os.open", side_effect=open_lock), mock.patch(
                "termdeck.tmux_backend.os.close", side_effect=close_lock
            ):
                legacy_path = _auth_legacy_lifetime_lock_path(record)
                canonical_path = _auth_lifetime_lock_path(record)
                lease = _acquire_auth_lifetime_lease(record)
                self.assertTrue(lease.matches(legacy_path, canonical_path))
                opened = [item for item in events if item[0] == "open"]
                self.assertEqual(opened, [("open", legacy_path), ("open", canonical_path)])
                opened_fds = [fd for fd, _path in lease._locks]
                lease.release()
                lease.release()

            closed = [item[1] for item in events if item[0] == "close"]
            self.assertEqual(closed, list(reversed(opened_fds)))

    def test_legacy_lifetime_holder_blocks_backend_before_canonical_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            codex_home = root / "auth-one"
            codex_home.mkdir()
            record = _record(auth_scope_id="auth-one", codex_home=str(codex_home))

            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
            ):
                legacy_path = _auth_legacy_lifetime_lock_path(record)
                legacy_path.parent.mkdir(parents=True, mode=0o700)
                holder_fd = os.open(legacy_path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(TmuxError, "still owns"):
                        _acquire_auth_lifetime_lease(record)
                    self.assertFalse(_auth_lifetime_lock_path(record).exists())
                finally:
                    fcntl.flock(holder_fd, fcntl.LOCK_UN)
                    os.close(holder_fd)

    def test_canonical_lifetime_holder_releases_legacy_on_backend_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_home = root / "cache"
            codex_home = root / "auth-one"
            codex_home.mkdir()
            record = _record(auth_scope_id="auth-one", codex_home=str(codex_home))

            with mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(cache_home)}, clear=False
            ):
                canonical_path = _auth_lifetime_lock_path(record)
                canonical_path.parent.mkdir(parents=True, mode=0o700)
                holder_fd = os.open(canonical_path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(TmuxError, "still owns"):
                        _acquire_auth_lifetime_lease(record)
                    legacy_path = _auth_legacy_lifetime_lock_path(record)
                    probe_fd = os.open(legacy_path, os.O_RDWR | os.O_APPEND)
                    try:
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        fcntl.flock(probe_fd, fcntl.LOCK_UN)
                        os.close(probe_fd)
                finally:
                    fcntl.flock(holder_fd, fcntl.LOCK_UN)
                    os.close(holder_fd)

    def test_auth_transaction_lock_is_reentrant_in_the_same_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            events = []

            with auth_transaction_lock(record):
                events.append("outer")
                with auth_transaction_lock(record):
                    events.append("inner")
            with auth_transaction_lock(record):
                events.append("reacquired")

            self.assertEqual(events, ["outer", "inner", "reacquired"])

    def test_auth_transaction_lock_rejects_concurrent_thread_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            codex_home.mkdir()
            record = _record(
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            outcomes = []

            def contend():
                try:
                    with auth_transaction_lock(record):
                        outcomes.append("acquired")
                except AuthTransactionBusy as exc:
                    outcomes.append(str(exc))

            with auth_transaction_lock(record):
                thread = threading.Thread(target=contend, daemon=True)
                thread.start()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(outcomes), 1)
            self.assertIn("already running", outcomes[0])

    def test_launcher_lifetime_holder_blocks_pointer_mutation_with_matching_path(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState
        from termdeck.tmux_backend import (
            _auth_lifetime_lock_path,
            execute_auth_change,
        )

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-alpha_beta_one"
            codex_home.mkdir()
            root = codex_home / ".termdeck-codex-lifetime"
            scope_id = "auth-alpha_beta_one"
            record = _record(
                status="stopped",
                auth_scope_id=scope_id,
                codex_home=str(codex_home),
            )
            helper = (
                Path(__file__).resolve().parents[1]
                / "terminal-auth"
                / "codex-contained-gates.sh"
            )
            environment = {
                **os.environ,
                "CODEX_TERMDECK_GATE_ROOT": str(root),
                "CODEX_HOME": str(codex_home),
                "TERMDECK_AUTH_SCOPE_ID": scope_id,
                "XDG_CACHE_HOME": str(Path(tmp) / "cache"),
            }
            path_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -e; source "$1"; '
                    'gate_root="$(codex_contained_gate_root "$(id -u)")"; '
                    'scope_key="$(codex_contained_scope_key "$2")"; '
                    'printf "%s/scope.%s.lock\\n" "$gate_root" "$scope_key"',
                    "gate-path-test",
                    str(helper),
                    scope_id,
                ],
                text=True,
                capture_output=True,
                timeout=5,
                env=environment,
                check=True,
            )
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    _auth_lifetime_lock_path(record),
                    Path(path_result.stdout.strip()),
                )

            holder = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'source "$1"; '
                    'codex_contained_acquire_termdeck_lifetime_lock '
                    '"$(id -u)" "$2" "$3"; '
                    'printf "READY\\n"; sleep 30',
                    "gate-holder-test",
                    str(helper),
                    scope_id,
                    str(Path(tmp)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "READY")
                previous = CodexAuthState(
                    codex_home=str(codex_home),
                    accounts=(CodexAccount("alpha", alias="alpha"),),
                    selected_account=None,
                    selected_account_key=None,
                    fingerprint="unselected",
                    codex_auth_available=True,
                )
                manager = mock.Mock()
                backend = TmuxBackend(executable="tmux")
                with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                    backend, "_auth_session_exists", return_value=False
                ):
                    with auth_transaction_lock(record):
                        result = execute_auth_change(
                            backend,
                            manager,
                            record,
                            previous,
                            "alpha",
                        )

                self.assertFalse(result.ok)
                self.assertIn("live owner", result.error)
                manager.switch.assert_not_called()
            finally:
                try:
                    os.killpg(holder.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                holder.wait(timeout=5)

    def test_private_create_or_resume_runs_inside_auth_transaction_lock(self):
        record = _record(
            auth_scope_id="auth-one",
            codex_home="/private/auth-one",
        )
        backend = TmuxBackend(executable="tmux")
        events = []
        lease = mock.Mock(name="lifetime_lease", held=True)
        lease.release.side_effect = lambda: events.append("lease-release")

        @contextmanager
        def transaction(candidate):
            self.assertIs(candidate, record)
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        with mock.patch(
            "termdeck.tmux_backend.auth_transaction_lock",
            side_effect=transaction,
        ) as lock, mock.patch.object(
            backend,
            "_auth_session_exists",
            return_value=False,
        ) as exists, mock.patch(
            "termdeck.tmux_backend._acquire_auth_lifetime_lease",
            side_effect=lambda candidate: (
                events.append(("lease-acquire", candidate)) or lease
            ),
        ) as acquire, mock.patch.object(
            backend,
            "_create_or_resume_unlocked",
            side_effect=lambda candidate, **kwargs: events.append(
                ("create", candidate, kwargs)
            ),
        ) as create:
            backend.create_or_resume(record)

        self.assertEqual(
            events,
            [
                "lock-enter",
                ("lease-acquire", record),
                (
                    "create",
                    record,
                    {"require_absent": True, "lifetime_lease": lease},
                ),
                "lease-release",
                "lock-exit",
            ],
        )
        lock.assert_called_once_with(record)
        exists.assert_called_once_with(record)
        acquire.assert_called_once_with(record)
        create.assert_called_once_with(
            record,
            require_absent=True,
            lifetime_lease=lease,
        )

    def test_private_recovery_pending_live_target_repairs_idle_placeholder(self):
        recovery = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
        record = _auth_record(
            status="recovery-pending",
            recovery_command=recovery,
            recovery_cwd="/tmp",
        )
        backend = TmuxBackend(executable="tmux")

        @contextmanager
        def transaction(candidate):
            self.assertIs(candidate, record)
            yield

        with mock.patch(
            "termdeck.tmux_backend.auth_transaction_lock",
            side_effect=transaction,
        ), mock.patch.object(
            backend, "_auth_session_exists", return_value=True
        ), mock.patch.object(
            backend, "_recover_exact_over_idle_placeholder"
        ) as recover, mock.patch.object(
            backend, "_create_or_resume_unlocked"
        ) as create:
            backend.create_or_resume(record)

        recover.assert_called_once_with(record)
        create.assert_not_called()

    def test_private_auth_path_drops_another_terminal_scope(self):
        home = Path("/private/terminal-auth/auth-this")
        with mock.patch.dict(
            os.environ,
            {
                "PATH": (
                    "/private/terminal-auth/auth-other/bin:"
                    "/usr/local/bin:/private/terminal-auth/auth-this/bin:/usr/bin"
                )
            },
        ):
            result = TmuxBackend._private_auth_path(home)

        self.assertEqual(
            result,
            "/private/terminal-auth/auth-this/bin:/usr/local/bin:/usr/bin",
        )

    def test_exact_recovery_respawns_same_idle_pane_without_tmux_gap(self):
        recovery = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
        record = _auth_record(
            command="/bin/bash",
            status="recovery-pending",
            recovery_command=recovery,
            recovery_cwd="/tmp",
        )
        backend = TmuxBackend(executable="tmux")
        previous = _auth_pane_identity(pane_pid=100)
        created = _auth_pane_identity(pane_pid=200)
        suspended = ((100, 1, False),)
        events = []
        lease = mock.Mock(name="lifetime_lease")
        lease.release.side_effect = lambda: events.append("lease-release")

        def run(argv, **_kwargs):
            events.append(("run", argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.dict(
            os.environ,
            {"PATH": "/tmp/other-scope/bin:/usr/bin"},
        ), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.os.access", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=previous
        ), mock.patch.object(
            backend, "_auth_process_identity", return_value=("S", 1)
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, None, None),
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=True
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=suspended
        ), mock.patch(
            "termdeck.tmux_backend._acquire_auth_lifetime_lease",
            return_value=lease,
        ), mock.patch(
            "termdeck.tmux_backend.migrate_quiescent_codex_configuration"
        ), mock.patch(
            "termdeck.tmux_backend.ensure_private_codex_state"
        ), mock.patch.object(
            backend, "_hydrate_private_codex_recovery"
        ) as hydrate, mock.patch.object(
            backend, "_run", side_effect=run
        ), mock.patch.object(
            backend, "_kill_quiesced_auth_processes"
        ) as kill_old, mock.patch.object(
            backend,
            "_await_exact_pane_respawn_ready",
            return_value=(created, ""),
        ) as await_ready, _auth_input_gate(backend):
            backend._recover_exact_over_idle_placeholder(record)

        self.assertEqual(events[0], "lease-release")
        respawn = events[1][1]
        self.assertEqual(respawn[:5], ["respawn-pane", "-k", "-t", "%1", "-e"])
        self.assertIn("CODEX_HOME=/tmp/auth-test", respawn)
        self.assertIn("TERMDECK_AUTH_SCOPE_ID=auth-test", respawn)
        self.assertIn("CODEX_TERMDECK_GATE_ROOT=/tmp/auth-test/.termdeck-codex-lifetime", respawn)
        self.assertEqual(respawn[-3:-1], ["-c", "/tmp"])
        self.assertIn("/tmp/auth-test/bin/codex", respawn[-1])
        self.assertIn("TERMDECK_AUTH_SCOPE_ID=auth-test", respawn[-1])
        self.assertIn("--dangerously-bypass-hook-trust", respawn[-1])
        self.assertNotIn("/tmp/other-scope/bin/codex", respawn[-1])
        self.assertIn("; exec ", respawn[-1])
        self.assertIn("bash --noprofile --rcfile", respawn[-1])
        self.assertIn("/tmp/auth-test/bin/.termdeck-bashrc", respawn[-1])
        self.assertTrue(respawn[-1].endswith(" -i"))
        hydrate.assert_called_once_with(record, recovery)
        kill_old.assert_called_once_with(suspended)
        await_ready.assert_called_once_with(
            record,
            previous=previous,
            expected_recovery_command=recovery,
            expected_recovery_cwd="/tmp",
        )

    def test_exact_recovery_leaves_a_different_live_conversation_untouched(self):
        recovery = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
        other = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd155"
        record = _auth_record(
            status="recovery-pending",
            recovery_command=recovery,
            recovery_cwd="/tmp",
        )
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_auth_process_identity", return_value=("S", 1)
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, other, "/tmp"),
        ), self.assertRaisesRegex(
            TmuxError, "different exact recovery session"
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree"
        ) as suspend, mock.patch.object(
            backend, "_run"
        ) as run:
            backend._recover_exact_over_idle_placeholder(record)

        suspend.assert_not_called()
        run.assert_not_called()

    def test_exact_recovery_refuses_placeholder_with_background_job(self):
        recovery = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
        record = _auth_record(
            status="recovery-pending",
            recovery_command=recovery,
            recovery_cwd="/tmp",
        )
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_auth_process_identity", return_value=("S", 1)
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, None, None),
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=False
        ), self.assertRaisesRegex(
            TmuxError, "idle placeholder shell with no jobs"
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree"
        ) as suspend, mock.patch.object(
            backend, "_run"
        ) as run:
            backend._recover_exact_over_idle_placeholder(record)

        suspend.assert_not_called()
        run.assert_not_called()

    def test_private_auth_environment_ready_requires_exact_live_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            gate_root = codex_home / ".termdeck-codex-lifetime"
            record = _record(
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_HOME": str(codex_home.resolve()),
                    "TERMDECK_AUTH_SCOPE_ID": "auth-one",
                    "CODEX_TERMDECK_GATE_ROOT": str(gate_root),
                    "PATH": f"{codex_home.resolve() / 'bin'}:{environment.get('PATH', '')}",
                }
            )
            process = subprocess.Popen(["/bin/sleep", "30"], env=environment)
            try:
                pane = _auth_pane_identity(command="sleep", pane_pid=process.pid)
                with mock.patch.dict(
                    os.environ,
                    {"CODEX_TERMDECK_GATE_ROOT": str(gate_root)},
                    clear=False,
                ), mock.patch(
                    "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
                ), mock.patch.object(
                    backend, "_inspect_auth_pane", return_value=pane
                ) as inspect:
                    self.assertTrue(backend.private_auth_environment_ready(record))

                inspect.assert_called_once_with(record)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_private_auth_environment_ready_rejects_pre_isolation_live_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "auth-one"
            record = _record(
                auth_scope_id="auth-one",
                codex_home=str(codex_home),
            )
            backend = TmuxBackend(executable="tmux")
            legacy_environment = os.environ.copy()
            legacy_environment.pop("CODEX_HOME", None)
            legacy_environment.pop("TERMDECK_AUTH_SCOPE_ID", None)
            wrong_environment = os.environ.copy()
            wrong_environment.update(
                {
                    "CODEX_HOME": str(codex_home.resolve()),
                    "TERMDECK_AUTH_SCOPE_ID": "auth-other",
                    "PATH": f"{codex_home.resolve() / 'bin'}:{wrong_environment.get('PATH', '')}",
                }
            )
            legacy = subprocess.Popen(["/bin/sleep", "30"], env=legacy_environment)
            wrong = subprocess.Popen(["/bin/sleep", "30"], env=wrong_environment)
            try:
                panes = [
                    _auth_pane_identity(command="sleep", pane_pid=legacy.pid),
                    _auth_pane_identity(command="sleep", pane_pid=wrong.pid),
                ]
                with mock.patch(
                    "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
                ), mock.patch.object(
                    backend, "_inspect_auth_pane", side_effect=panes
                ):
                    self.assertFalse(backend.private_auth_environment_ready(record))
                    self.assertFalse(backend.private_auth_environment_ready(record))
            finally:
                legacy.terminate()
                wrong.terminate()
                legacy.wait(timeout=5)
                wrong.wait(timeout=5)

    def test_auth_recreation_readiness_accepts_exact_recovery_takeover(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        recovery = f"codex --yolo resume {session_id}"
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(
            command="codex",
            session_created="1700000001",
            pane_pid=4242,
        )

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            TmuxBackend, "_auth_process_identity", return_value=("S", 22)
        ), mock.patch.object(
            TmuxBackend,
            "_private_auth_process_tree_environment_ready",
            return_value=True,
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, recovery, "/workspace/repo"),
        ) as observe:
            ready, detail = backend._await_auth_recreation_ready(
                record,
                previous_session_created="1700000000",
                previous_pane_id="%1",
                previous_pane_pid=100,
                previous_pane_start_time=1,
                expected_recovery_command=recovery,
                expected_recovery_cwd="/workspace/repo",
                timeout=0.05,
            )

        self.assertEqual(ready, identity)
        self.assertEqual(detail, "")
        observe.assert_called_once_with(4242)

    def test_auth_recreation_accepts_same_second_with_fresh_pane_process_identity(self):
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(
            session_created="1700000000",
            pane_id="%2",
            pane_pid=4242,
        )

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            TmuxBackend, "_auth_process_identity", return_value=("S", 22)
        ), mock.patch.object(
            TmuxBackend,
            "_private_auth_process_tree_environment_ready",
            return_value=True,
        ):
            ready, detail = backend._await_auth_recreation_ready(
                record,
                previous_session_created="1700000000",
                previous_pane_id="%1",
                previous_pane_pid=100,
                previous_pane_start_time=1,
                expected_recovery_command=None,
                expected_recovery_cwd=None,
                timeout=0.05,
            )

        self.assertEqual(ready, identity)
        self.assertEqual(detail, "")

    def test_auth_recreation_rejects_the_complete_previous_identity(self):
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(
            session_created="1700000000",
            pane_id="%1",
            pane_pid=100,
        )

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            TmuxBackend, "_auth_process_identity", return_value=("S", 1)
        ), mock.patch(
            "termdeck.tmux_backend.time.monotonic", side_effect=[100.0, 101.0]
        ), mock.patch("termdeck.tmux_backend.time.sleep") as sleep:
            ready, detail = backend._await_auth_recreation_ready(
                record,
                previous_session_created="1700000000",
                previous_pane_id="%1",
                previous_pane_pid=100,
                previous_pane_start_time=1,
                expected_recovery_command=None,
                expected_recovery_cwd=None,
                timeout=0.05,
            )

        self.assertEqual(ready, identity)
        self.assertIn("session, pane, and process identity", detail)
        sleep.assert_not_called()

    def test_failed_auth_recreation_cleanup_waits_for_exact_process_exit(self):
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(pane_pid=4242)

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ) as terminate, mock.patch.object(
            backend, "_auth_session_exists", return_value=False
        ), mock.patch.object(
            backend,
            "_auth_process_identity",
            side_effect=[("S", 22), ("S", 22), None],
        ), mock.patch("termdeck.tmux_backend.time.sleep") as sleep:
            cleaned, detail = backend._cleanup_failed_auth_recreation(
                record, identity, timeout=0.5
            )

        self.assertTrue(cleaned)
        self.assertEqual(detail, "")
        terminate.assert_called_once_with(record)
        sleep.assert_called_once_with(0.025)

    def test_failed_auth_recreation_cleanup_accepts_pid_reuse_as_old_process_exit(self):
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(pane_pid=4242)

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ), mock.patch.object(
            backend, "_auth_session_exists", return_value=False
        ), mock.patch.object(
            backend,
            "_auth_process_identity",
            side_effect=[("S", 22), ("S", 23)],
        ):
            cleaned, detail = backend._cleanup_failed_auth_recreation(
                record, identity, timeout=0.5
            )

        self.assertTrue(cleaned)
        self.assertEqual(detail, "")

    def test_failed_auth_recreation_cleanup_fails_when_exact_process_stays_live(self):
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(pane_pid=4242)

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ), mock.patch.object(
            backend, "_auth_session_exists", return_value=False
        ), mock.patch.object(
            backend, "_auth_process_identity", return_value=("S", 22)
        ), mock.patch(
            "termdeck.tmux_backend.time.monotonic", side_effect=[100.0, 101.0]
        ), mock.patch("termdeck.tmux_backend.time.sleep") as sleep:
            cleaned, detail = backend._cleanup_failed_auth_recreation(
                record, identity, timeout=0.05
            )

        self.assertFalse(cleaned)
        self.assertIn("process remained live", detail)
        sleep.assert_not_called()

    def test_auth_recreation_readiness_rejects_wrong_recovery_takeover(self):
        expected_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        wrong_id = "019f71b3-fa32-7251-bac3-c628f34dd155"
        record = _auth_record(status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(
            command="codex",
            session_created="1700000001",
            pane_pid=4242,
        )

        with mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            TmuxBackend, "_auth_process_identity", return_value=("S", 22)
        ), mock.patch.object(
            TmuxBackend,
            "_private_auth_process_tree_environment_ready",
            return_value=True,
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(
                True,
                f"codex --yolo resume {wrong_id}",
                "/workspace/repo",
            ),
        ), mock.patch(
            "termdeck.tmux_backend.time.monotonic", side_effect=[100.0, 101.0]
        ), mock.patch("termdeck.tmux_backend.time.sleep") as sleep:
            ready, detail = backend._await_auth_recreation_ready(
                record,
                previous_session_created="1700000000",
                previous_pane_id="%1",
                previous_pane_pid=100,
                previous_pane_start_time=1,
                expected_recovery_command=f"codex --yolo resume {expected_id}",
                expected_recovery_cwd="/workspace/repo",
                timeout=0.05,
            )

        self.assertEqual(ready, identity)
        self.assertIn("exact Codex recovery UUID", detail)
        sleep.assert_not_called()

    def test_auth_change_restarts_exact_live_codex_and_preserves_resume_identity(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        recovery = f"codex --yolo resume {session_id}"

        identity = _auth_pane_identity(command="codex")
        created_identity = _auth_pane_identity(
            command="codex", session_created="1700000001", pane_pid=4242
        )
        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            side_effect=[
                (True, recovery, "/workspace/repo"),
                (True, recovery, "/workspace/repo"),
                (True, recovery, "/workspace/repo"),
            ],
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ), mock.patch.object(
            backend, "_kill_quiesced_auth_processes"
        ), mock.patch.object(backend, "_terminate_auth_session") as terminate, mock.patch.object(
            backend, "_create_or_resume_unlocked"
        ) as create, mock.patch.object(
            backend,
            "_await_auth_recreation_ready",
            return_value=(created_identity, ""),
        ), _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertTrue(result.ok)
        self.assertTrue(result.restarted)
        self.assertEqual(result.recovery_command, recovery)
        self.assertEqual(result.recovery_cwd, "/workspace/repo")
        self.assertEqual(result.pid, 4242)
        self.assertEqual(result.status, "running")
        self.assertFalse(result.clear_recovery)
        self.assertIsNone(result.command)
        self.assertIsNone(result.cwd)
        terminate.assert_called_once_with(record)
        resumed = create.call_args.args[0]
        self.assertTrue(create.call_args.kwargs["require_absent"])
        self.assertEqual(resumed.recovery_command, recovery)
        self.assertEqual(resumed.recovery_cwd, "/workspace/repo")
        self.assertEqual(resumed.status, "running")

    def test_auth_change_idle_shell_needs_no_restart(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")

        identity = _auth_pane_identity()
        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ) as suspend, mock.patch.object(
            backend, "_resume_auth_processes"
        ) as resume, mock.patch.object(
            backend, "create_or_resume"
        ) as create, _stable_auth_process(backend, environment_ready=True), _auth_input_gate(
            backend
        ) as input_gate:
            result = backend.activate_auth_change(record)

        self.assertTrue(result.ok)
        self.assertFalse(result.restarted)
        self.assertIn("next Codex launch", result.detail)
        suspend.assert_called_once_with(100, allow_running_root=True)
        input_gate[0].assert_called_once_with("%1")
        input_gate[1].assert_called_once_with("%1", False)
        resume.assert_called_once_with(((100, 1, False),))
        create.assert_not_called()

    def test_auth_change_records_paused_state_when_post_suspend_rollback_fails(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")

        identity = _auth_pane_identity()
        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend,
            "_inspect_auth_pane",
            side_effect=[identity, identity, TmuxError("tmux inspection failed")],
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ), mock.patch.object(
            backend, "_resume_auth_processes", side_effect=TmuxError("resume denied")
        ), _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.pid, 100)
        self.assertIn("could not be fully resumed", result.detail)

    def test_partial_auth_resume_failure_re_stops_already_resumed_processes(self):
        processes = ((100, 10, False), (101, 11, False))
        states = {100: "T", 101: "T"}

        def identity(pid):
            return states[pid], 10 if pid == 100 else 11

        def send_signal(pid, requested_signal):
            if (pid, requested_signal) == (100, signal.SIGCONT):
                raise PermissionError("resume denied")
            if requested_signal == signal.SIGCONT:
                states[pid] = "R"
            elif requested_signal == signal.SIGSTOP:
                states[pid] = "T"

        with mock.patch.object(
            TmuxBackend, "_auth_process_identity", side_effect=identity
        ), mock.patch(
            "termdeck.tmux_backend.os.kill", side_effect=send_signal
        ) as kill, self.assertRaisesRegex(TmuxError, "permission denied"):
            TmuxBackend._resume_auth_processes(processes)

        self.assertEqual(states[101], "T")
        self.assertEqual(
            kill.call_args_list,
            [
                mock.call(101, signal.SIGCONT),
                mock.call(100, signal.SIGCONT),
                mock.call(101, signal.SIGSTOP),
            ],
        )

    def test_auth_change_migrates_legacy_idle_shell_using_live_directory(self):
        record = _auth_record(
            command="python old-task.py",
            cwd="/saved",
            status="running",
            recovery_command=(
                "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
            ),
            recovery_cwd="/stale-codex-cwd",
        )
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(cwd="/tmp")
        created_identity = _auth_pane_identity(
            cwd="/tmp", session_created="1700000001", pane_pid=4243
        )

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_idle_shell_executable", return_value="/bin/bash"
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ), mock.patch.object(
            backend, "_kill_quiesced_auth_processes"
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ) as terminate, mock.patch.object(
            backend, "_create_or_resume_unlocked"
        ) as create, mock.patch.object(
            backend,
            "_await_auth_recreation_ready",
            return_value=(created_identity, ""),
        ), _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)
            quiescence = backend.quiesce_auth_change(record, plan)
            result = backend.apply_auth_change(record, plan, quiescence)

        self.assertTrue(plan.ok)
        self.assertTrue(plan.restart)
        self.assertFalse(plan.environment_ready)
        self.assertEqual(plan.replacement_command, "/bin/bash")
        self.assertEqual(plan.replacement_cwd, "/tmp")
        self.assertTrue(result.ok)
        self.assertTrue(result.restarted)
        self.assertTrue(result.preserve_selected_account)
        self.assertEqual(result.command, "/bin/bash")
        self.assertEqual(result.cwd, "/tmp")
        self.assertTrue(result.clear_recovery)
        self.assertIsNone(result.recovery_command)
        self.assertIsNone(result.recovery_cwd)
        terminate.assert_called_once_with(record)
        migrated = create.call_args.args[0]
        self.assertTrue(create.call_args.kwargs["require_absent"])
        self.assertEqual(migrated.command, "/bin/bash")
        self.assertEqual(migrated.cwd, "/tmp")
        self.assertIsNone(migrated.recovery_command)
        self.assertIsNone(migrated.recovery_cwd)

    def test_failed_legacy_recreation_returns_durable_shell_replacement_metadata(self):
        record = _auth_record(
            command="python stale-task.py",
            cwd="/saved",
            status="running",
            recovery_command=(
                "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"
            ),
            recovery_cwd="/stale-codex-cwd",
        )
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(cwd="/tmp")

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=False
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_idle_shell_executable", return_value="/bin/bash"
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ), mock.patch.object(
            backend, "_kill_quiesced_auth_processes"
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ), mock.patch.object(
            backend,
            "_create_or_resume_unlocked",
            side_effect=TmuxError("recreate failed"),
        ), _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "stopped")
        self.assertTrue(result.preserve_selected_account)
        self.assertEqual(result.command, "/bin/bash")
        self.assertEqual(result.cwd, "/tmp")
        self.assertTrue(result.clear_recovery)
        self.assertIsNone(result.recovery_command)
        self.assertIsNone(result.recovery_cwd)
        self.assertIn("idle shell", result.detail)

    def test_auth_change_rejects_legacy_shell_with_background_job_at_preflight(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=False
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_idle_shell_executable", return_value="/bin/bash"
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=False
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "terminate"
        ) as terminate, _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)

        self.assertFalse(plan.ok)
        self.assertIn("background job", plan.detail)
        terminate.assert_not_called()

    def test_legacy_bash_with_background_sleep_is_rejected_and_left_running(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        shell = subprocess.Popen(
            ["bash", "-c", "sleep 30 & wait"],
            start_new_session=True,
        )
        try:
            tree = []
            for _ in range(100):
                tree = process_tree(shell.pid)
                if len(tree) > 1:
                    break
                time.sleep(0.01)
            self.assertGreater(len(tree), 1)
            identity = _auth_pane_identity(pane_pid=shell.pid)
            with mock.patch.object(
                backend, "_auth_session_exists", return_value=True
            ), mock.patch.object(
                backend, "_private_auth_process_environment_ready", return_value=False
            ), mock.patch.object(
                backend, "_inspect_auth_pane", return_value=identity
            ), mock.patch.object(
                backend, "terminate"
            ) as terminate:
                plan = backend.plan_auth_change(record)

            self.assertFalse(plan.ok)
            self.assertIn("background job", plan.detail)
            self.assertIsNone(shell.poll())
            terminate.assert_not_called()
        finally:
            try:
                os.killpg(shell.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            shell.wait(timeout=5)

    def test_auth_change_rejects_background_job_started_after_preflight(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=False
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_idle_shell_executable", return_value="/bin/bash"
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", side_effect=[True, False]
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ) as terminate, _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)
            quiescence = backend.quiesce_auth_change(record, plan)

        self.assertTrue(plan.ok)
        self.assertFalse(quiescence.ok)
        self.assertIn("background job after auth preflight", quiescence.detail)
        terminate.assert_not_called()

    def test_auth_change_revalidates_identity_before_legacy_termination(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()
        changed_identity = _auth_pane_identity(pane_pid=101)

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=False
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
        ), mock.patch.object(
            backend, "_idle_shell_executable", return_value="/bin/bash"
        ), mock.patch.object(
            backend, "_process_tree_is_idle_shell", return_value=True
        ), mock.patch.object(
            backend,
            "_inspect_auth_pane",
            side_effect=[identity, changed_identity],
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ) as terminate, _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)
            quiescence = backend.quiesce_auth_change(record, plan)

        self.assertTrue(plan.ok)
        self.assertFalse(quiescence.ok)
        self.assertIn("changed after auth preflight", quiescence.detail)
        terminate.assert_not_called()

    def test_auth_change_reobserves_exact_recovery_before_termination(self):
        first_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        second_id = "019f71b3-fa32-7251-bac3-c628f34dd155"
        first = f"codex --yolo resume {first_id}"
        second = f"codex --yolo resume {second_id}"
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(command="codex")

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            side_effect=[
                (True, first, "/workspace/repo"),
                (True, second, "/workspace/repo"),
            ],
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch.object(
            backend,
            "_inspect_auth_pane",
            return_value=identity,
        ), mock.patch.object(
            backend, "_terminate_auth_session"
        ) as terminate, _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)
            quiescence = backend.quiesce_auth_change(record, plan)

        self.assertTrue(plan.ok)
        self.assertEqual(plan.recovery_command, first)
        self.assertFalse(quiescence.ok)
        self.assertIn("recovery identity changed", quiescence.detail)
        terminate.assert_not_called()

    def test_auth_change_rejects_external_session_without_inspection(self):
        record = _auth_record(external=True, pane_target="%9")
        backend = TmuxBackend(executable="tmux")

        with mock.patch.object(backend, "session_exists") as exists, mock.patch.object(
            backend, "_run"
        ) as run:
            plan = backend.plan_auth_change(record)

        self.assertFalse(plan.ok)
        self.assertIn("external", plan.detail)
        exists.assert_not_called()
        run.assert_not_called()

    def test_auth_change_allows_stopped_managed_terminal_without_recreation(self):
        record = _auth_record(status="stopped")
        backend = TmuxBackend(executable="tmux")

        with mock.patch.object(backend, "_auth_session_exists", return_value=False), mock.patch.object(
            backend, "terminate"
        ) as terminate, mock.patch.object(
            backend, "create_or_resume"
        ) as create, _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            plan = backend.plan_auth_change(record)
            quiescence = backend.quiesce_auth_change(record, plan)
            result = backend.apply_auth_change(record, plan, quiescence)

        self.assertTrue(plan.ok)
        self.assertFalse(plan.live)
        self.assertTrue(result.ok)
        self.assertFalse(result.restarted)
        terminate.assert_not_called()
        create.assert_not_called()

    def test_auth_change_fails_closed_when_live_codex_has_no_exact_resume_identity(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(command="codex")

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "terminate"
        ) as terminate, _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertFalse(result.restarted)
        self.assertIn("could not be captured exactly", result.detail)
        terminate.assert_not_called()

    def test_auth_change_rejects_non_exact_codex_recovery_before_killing_tab(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity(command="codex")

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, "codex --yolo", "/workspace/repo"),
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "terminate"
        ) as terminate, mock.patch.object(
            backend, "create_or_resume"
        ) as create, _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertFalse(result.restarted)
        self.assertIn("could not be captured exactly", result.detail)
        terminate.assert_not_called()
        create.assert_not_called()

    def test_auth_change_unknown_process_tree_fails_closed(self):
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        identity = _auth_pane_identity()

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata", return_value=(True, None, None)
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=False
        ), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=None
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "terminate"
        ) as terminate, _stable_auth_process(backend, environment_ready=False), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertFalse(result.restarted)
        self.assertIn("could not be captured exactly", result.detail)
        terminate.assert_not_called()

    def test_auth_change_persists_recovery_state_when_recreate_fails(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        recovery = f"codex --yolo resume {session_id}"
        identity = _auth_pane_identity(command="codex")

        with mock.patch.object(backend, "_auth_session_exists", return_value=True), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            side_effect=[
                (True, recovery, "/workspace/repo"),
                (True, recovery, "/workspace/repo"),
                (True, recovery, "/workspace/repo"),
            ],
        ), mock.patch.object(
            backend, "private_auth_environment_ready", return_value=True
        ), mock.patch.object(
            backend, "_inspect_auth_pane", return_value=identity
        ), mock.patch.object(
            backend, "_suspend_auth_process_tree", return_value=((100, 1, False),)
        ), mock.patch.object(
            backend, "_kill_quiesced_auth_processes"
        ), mock.patch.object(backend, "_terminate_auth_session"), mock.patch.object(
            backend,
            "_create_or_resume_unlocked",
            side_effect=TmuxError("recreate failed"),
        ), _stable_auth_process(backend, environment_ready=True), _auth_input_gate(backend):
            result = backend.activate_auth_change(record)

        self.assertFalse(result.ok)
        self.assertFalse(result.restarted)
        self.assertEqual(result.status, "recovery-pending")
        self.assertEqual(result.recovery_command, recovery)
        self.assertEqual(result.recovery_cwd, "/workspace/repo")
        self.assertTrue(result.preserve_selected_account)
        self.assertFalse(result.clear_recovery)
        self.assertIsNone(result.command)
        self.assertIsNone(result.cwd)
        self.assertIn("recreate failed", result.detail)

    def test_auth_change_marks_failed_recreation_uncertain_when_cleanup_cannot_bind(self):
        from termdeck.tmux_backend import AuthActivationPlan, AuthQuiesceResult

        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        recovery = f"codex --yolo resume {session_id}"
        record = _auth_record(command="/bin/bash", status="running")
        backend = TmuxBackend(executable="tmux")
        plan = AuthActivationPlan(
            True,
            live=True,
            restart=True,
            environment_ready=True,
            session_created="1700000000",
            pane_id="%1",
            pane_pid=100,
            pane_start_time=1,
            pane_current_command="codex",
            pane_current_path="/workspace/repo",
            recovery_command=recovery,
            recovery_cwd="/workspace/repo",
        )
        lease = mock.Mock(name="auth_lifetime_lease")
        quiescence = AuthQuiesceResult(
            True,
            live=True,
            terminated=True,
            plan_token=backend._auth_plan_token(plan),
            lifetime_lease=lease,
        )
        created = _auth_pane_identity(
            command="codex",
            cwd="/workspace/repo",
            session_created="1700000001",
            pane_pid=4242,
        )

        with mock.patch.object(
            backend, "_create_or_resume_unlocked"
        ) as create, mock.patch.object(
            backend,
            "_await_auth_recreation_ready",
            return_value=(created, "fresh pane has not taken over the exact Codex recovery UUID"),
        ), mock.patch.object(
            backend,
            "_cleanup_failed_auth_recreation",
            return_value=(False, "terminal identity changed before failed-restart cleanup"),
        ):
            result = backend.apply_auth_change(record, plan, quiescence)

        self.assertFalse(result.ok)
        self.assertFalse(result.restarted)
        self.assertEqual(result.status, "recovery-pending")
        self.assertEqual(result.pid, 4242)
        self.assertEqual(result.recovery_command, recovery)
        self.assertIn("exact Codex recovery UUID", result.detail)
        self.assertIn("identity changed", result.detail)
        self.assertEqual(lease.release.call_args_list, [mock.call(), mock.call()])
        create.assert_called_once()
        self.assertTrue(create.call_args.kwargs["require_absent"])

    def test_auth_change_apply_requires_the_matching_quiescence_receipt(self):
        from termdeck.tmux_backend import AuthActivationPlan, AuthQuiesceResult

        backend = TmuxBackend(executable="tmux")
        record = _auth_record(status="stopped")
        plan = AuthActivationPlan(True, detail="stopped")

        result = backend.apply_auth_change(
            record,
            plan,
            AuthQuiesceResult(True, detail="not bound to this plan"),
        )

        self.assertFalse(result.ok)
        self.assertIn("matching successful quiescence receipt", result.detail)

    def test_same_account_reselect_repairs_legacy_binding_after_quiescence(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
            execute_auth_change,
        )

        calls = []
        record = _auth_record(status="stopped", selected_account="alpha")
        stale = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(CodexAccount("alpha", alias="alpha", active=True),),
            selected_account="alpha",
            selected_account_key="alpha",
            fingerprint="stale",
            codex_auth_available=True,
            catalog_shared=False,
            canonical_binding_ready=False,
        )
        repaired = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(CodexAccount("alpha", alias="alpha", active=True),),
            selected_account="alpha",
            selected_account_key="alpha",
            fingerprint="repaired",
            codex_auth_available=True,
            canonical_binding_ready=True,
        )

        class Backend:
            def plan_auth_change(self, _record):
                calls.append("plan")
                return AuthActivationPlan(True, detail="stopped")

            def quiesce_auth_change(self, _record, _plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True)

            def apply_auth_change(self, _record, _plan, _quiescence):
                calls.append("apply")
                return AuthActivationResult(True, False)

        class Manager:
            def switch(self, _record, account_key):
                calls.append(("switch", account_key))
                return CodexSwitchResult(True, repaired)

            def restore(self, *_args):
                raise AssertionError("successful repair must not roll back")

        result = execute_auth_change(Backend(), Manager(), record, stale, "alpha")

        self.assertTrue(result.ok)
        self.assertEqual(calls, ["plan", "quiesce", ("switch", "alpha"), "apply"])
        self.assertEqual(result.switch_result.state.fingerprint, "repaired")

    def test_explicit_same_canonical_account_still_activates_a_legacy_terminal(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
            execute_auth_change,
        )

        calls = []
        record = _auth_record(status="running", selected_account="alpha")
        selected = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(CodexAccount("alpha", alias="alpha", active=True),),
            selected_account="alpha",
            selected_account_key="alpha",
            fingerprint="canonical",
            codex_auth_available=True,
            canonical_binding_ready=True,
        )

        class Backend:
            def plan_auth_change(self, _record):
                calls.append("plan")
                return AuthActivationPlan(True, live=True, restart=True)

            def quiesce_auth_change(self, _record, _plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, _record, _plan, _quiescence):
                calls.append("apply")
                return AuthActivationResult(True, True)

        class Manager:
            def switch(self, _record, account_key):
                calls.append(("switch", account_key))
                return CodexSwitchResult(True, selected)

        result = execute_auth_change(Backend(), Manager(), record, selected, "alpha")

        self.assertTrue(result.ok)
        self.assertEqual(calls, ["plan", "quiesce", ("switch", "alpha"), "apply"])

    def test_switch_failure_restores_unselected_pointer_before_reactivation(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
            execute_auth_change,
        )

        calls = []
        record = _auth_record(status="stopped", selected_account=None)
        previous = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(CodexAccount("alpha", alias="alpha", active=False),),
            selected_account=None,
            selected_account_key=None,
            fingerprint="unselected",
            codex_auth_available=True,
        )

        class Backend:
            def plan_auth_change(self, _record):
                calls.append("plan")
                return AuthActivationPlan(True, detail="stopped")

            def quiesce_auth_change(self, _record, _plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True)

            def apply_auth_change(self, _record, _plan, _quiescence):
                calls.append("apply")
                return AuthActivationResult(True, False)

        class Manager:
            def switch(self, _record, account_key):
                calls.append(("switch", account_key))
                return CodexSwitchResult(False, previous, "replace failed")

            def restore(self, _record, account_key):
                calls.append(("restore", account_key))
                return CodexSwitchResult(True, previous)

        result = execute_auth_change(Backend(), Manager(), record, previous, "alpha")

        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertTrue(result.activation.ok)
        self.assertEqual(
            calls,
            [
                "plan",
                "quiesce",
                ("switch", "alpha"),
                ("restore", None),
                "apply",
            ],
        )

    def test_failed_restore_leaves_terminated_terminal_stopped(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthQuiesceResult,
            execute_auth_change,
        )

        calls = []
        record = _auth_record(status="running", selected_account="alpha")
        previous = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(
                CodexAccount("alpha", alias="alpha", active=True),
                CodexAccount("beta", alias="beta", active=False),
            ),
            selected_account="alpha",
            selected_account_key="alpha",
            fingerprint="before",
            codex_auth_available=True,
        )

        class Backend:
            def plan_auth_change(self, _record):
                calls.append("plan")
                return AuthActivationPlan(
                    True,
                    live=True,
                    restart=True,
                    replacement_command="/bin/bash",
                    replacement_cwd="/tmp",
                )

            def quiesce_auth_change(self, _record, _plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, *_args):
                calls.append("apply")
                raise AssertionError("uncertain credentials must never be reactivated")

        class Manager:
            def switch(self, _record, account_key):
                calls.append(("switch", account_key))
                return CodexSwitchResult(False, previous, "replace failed")

            def restore(self, _record, account_key):
                calls.append(("restore", account_key))
                return CodexSwitchResult(False, previous, "restore not verified")

        result = execute_auth_change(Backend(), Manager(), record, previous, "beta")

        self.assertFalse(result.ok)
        self.assertFalse(result.rolled_back)
        self.assertFalse(result.activation.ok)
        self.assertEqual(result.activation.status, "stopped")
        self.assertNotIn("apply", calls)
        self.assertIn("restore failed", result.error)

    def test_restore_exception_and_unreadable_state_is_persisted_as_unverified(self):
        from termdeck.codex_auth import CodexAccount, CodexAuthState
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthQuiesceResult,
            execute_auth_change,
        )

        record = _auth_record(status="running", selected_account="alpha")
        previous = CodexAuthState(
            codex_home=record.codex_home or "",
            accounts=(
                CodexAccount("alpha", alias="alpha", active=True),
                CodexAccount("beta", alias="beta", active=False),
            ),
            selected_account="alpha",
            selected_account_key="alpha",
            fingerprint="before",
            codex_auth_available=True,
        )

        class Backend:
            def plan_auth_change(self, _record):
                return AuthActivationPlan(
                    True,
                    live=True,
                    restart=True,
                    replacement_command="/bin/bash",
                    replacement_cwd="/tmp",
                )

            def quiesce_auth_change(self, _record, _plan):
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, *_args):
                raise AssertionError("an unverified pointer must remain stopped")

        class Manager:
            def switch(self, *_args):
                raise OSError("partial pointer replace")

            def restore(self, *_args):
                raise OSError("restore failed")

            def state(self, *_args):
                raise OSError("state unreadable")

        result = execute_auth_change(Backend(), Manager(), record, previous, "beta")

        self.assertFalse(result.ok)
        self.assertIsNone(result.switch_result.state.selected_account)
        self.assertIsNone(result.switch_result.state.selected_account_key)
        self.assertFalse(result.switch_result.state.canonical_binding_ready)
        self.assertEqual(result.activation.status, "stopped")
        self.assertIn("could not be inspected", result.error)

    def test_checkpoint_records_uses_exact_codex_uuid_without_memory_image(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        checkpoints = FakeCheckpointManager()
        record = _record(recovery_command=f"codex --yolo resume {session_id}")
        backend = TmuxBackend(checkpoint_manager=checkpoints)

        with mock.patch.object(
            backend,
            "session_exists_many",
            return_value={record.id: True},
        ):
            results = backend.checkpoint_records([record])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "codex-resume")
        self.assertTrue(results[0].exact)
        self.assertEqual(checkpoints.checkpointed, [])

    def test_checkpoint_records_checkpoints_non_codex_process(self):
        checkpoints = FakeCheckpointManager()
        record = _record(command="python worker.py")
        backend = TmuxBackend(checkpoint_manager=checkpoints)

        with mock.patch.object(
            backend,
            "session_exists_many",
            return_value={record.id: True},
        ):
            results = backend.checkpoint_records([record])

        self.assertEqual(results[0].action, "checkpoint")
        self.assertTrue(results[0].exact)
        self.assertEqual(checkpoints.checkpointed, [record.id])

    def test_terminate_clears_stale_process_checkpoint(self):
        checkpoints = FakeCheckpointManager()
        record = _record()
        backend = TmuxBackend(checkpoint_manager=checkpoints)

        with mock.patch.object(backend, "session_exists", return_value=False):
            backend.terminate(record)

        self.assertEqual(checkpoints.cleared, [record.id])

    def test_recovery_command_observation_skips_external_sessions_and_unknown_pids(self):
        internal = _record(id="internal", tmux_name="internal")
        missing = _record(id="missing", tmux_name="missing")
        external = _record(id="external", tmux_name="external", external=True)
        backend = TmuxBackend(executable="tmux")

        with mock.patch.object(
            backend,
            "pane_pids",
            return_value={"internal": 101, "missing": None, "external": 303},
        ), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, "codex --yolo", "/workspace/internal"),
        ) as observe:
            metadata = backend.recovery_metadata([internal, missing, external])

        self.assertEqual(
            metadata,
            {"internal": ("codex --yolo", "/workspace/internal")},
        )
        observe.assert_called_once_with(101)

    def test_recovery_command_observation_round_robins_with_a_bounded_scan(self):
        records = [
            _record(id=f"record-{index}", tmux_name=f"record-{index}")
            for index in range(5)
        ]
        backend = TmuxBackend(executable="tmux", recovery_scan_limit=2)
        pids = {record.id: 100 + index for index, record in enumerate(records)}

        with mock.patch.object(backend, "pane_pids", return_value=pids), mock.patch(
            "termdeck.tmux_backend.observe_recovery_metadata",
            return_value=(True, "codex --yolo", "/workspace"),
        ) as observe:
            first = backend.recovery_metadata(records)
            second = backend.recovery_metadata(records)

        self.assertEqual(set(first), {"record-0", "record-1"})
        self.assertEqual(set(second), {"record-2", "record-3"})
        self.assertEqual(
            [call.args[0] for call in observe.call_args_list],
            [100, 101, 102, 103],
        )

    def test_scroll_down_at_primary_screen_live_bottom_does_not_enter_history(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="0\t0\tbash\t0\t0\n", stderr=""
        )

        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=context) as run:
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.disposition, ScrollDisposition.UNHANDLED)
        self.assertIs(outcome.edge, EdgeKnowledge.LIVE_EXACT)
        self.assertEqual(outcome.depth, 0)
        self.assertFalse(backend.scroll_view_active(record))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "display-message")

    def test_scroll_codex_opens_transcript_before_navigating(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        command = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(
            backend, "_run", side_effect=[command, completed, completed]
        ) as run:
            outcome = backend.scroll_session(record, -3)

        self.assertIs(outcome.disposition, ScrollDisposition.HANDLED)
        self.assertIs(outcome.owner, HistoryOwner.CODEX)
        self.assertIs(outcome.edge, EdgeKnowledge.HISTORY_EXACT)
        self.assertEqual(outcome.depth, 3)
        self.assertTrue(outcome.managed_open)
        state = backend.scroll_state(record)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.managed_open)
        self.assertEqual(state.depth, 3)
        self.assertEqual(
            run.call_args_list[1],
            mock.call(
                ["send-keys", "-t", backend.pane_target(record), "C-t"],
                check=False,
                timeout=backend.query_timeout,
            ),
        )
        self.assertEqual(
            run.call_args_list[2],
            mock.call(
                [
                    "send-keys",
                    "-t",
                    backend.pane_target(record),
                    "-N",
                    "3",
                    "Up",
                ],
                check=False,
                timeout=backend.query_timeout,
            ),
        )

    def test_scroll_shell_uses_tmux_copy_mode_and_reports_exact_position(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        command = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="0\t0\tbash\t0\t0\n", stderr=""
        )
        scrolled = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t3\t120\n", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[command, scrolled]) as run:
            outcome = backend.scroll_session(record, -3)

        self.assertIs(outcome.disposition, ScrollDisposition.HANDLED)
        self.assertIs(outcome.owner, HistoryOwner.TMUX)
        self.assertIs(outcome.edge, EdgeKnowledge.HISTORY_EXACT)
        self.assertEqual(outcome.depth, 3)
        self.assertEqual(outcome.extent, 120)
        self.assertTrue(backend.scroll_view_active(record))
        self.assertIn("copy-mode", run.call_args_list[-1].args[0])
        self.assertIn("scroll-up", run.call_args_list[-1].args[0])
        self.assertIn("#{pane_in_mode}\t#{scroll_position}\t#{history_size}", run.call_args_list[-1].args[0])

    def test_single_scroll_down_from_deep_tmux_history_is_incremental(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.TMUX,
            depth=30,
            extent=120,
            command="bash",
            validated_at=time.monotonic(),
        )
        scrolled = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t27\t120\n", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=scrolled) as run:
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.edge, EdgeKnowledge.HISTORY_EXACT)
        self.assertEqual(outcome.depth, 27)
        self.assertTrue(backend.scroll_view_active(record))
        self.assertIn("scroll-down", run.call_args.args[0])
        self.assertNotIn("copy-mode", run.call_args.args[0])

    def test_single_scroll_down_from_deep_codex_history_does_not_close_transcript(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[context, completed]) as run:
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.edge, EdgeKnowledge.HISTORY_EXACT)
        self.assertEqual(outcome.depth, 27)
        self.assertTrue(backend.scroll_view_active(record))
        self.assertEqual(
            run.call_args_list[-1].args[0],
            [
                "send-keys",
                "-t",
                backend.pane_target(record),
                "-N",
                "3",
                "Down",
            ],
        )
        self.assertNotIn("C-t", run.call_args.args[0])

    def test_codex_scroll_down_reports_live_only_when_logical_depth_reaches_zero(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=3,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[context, completed]):
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.edge, EdgeKnowledge.LIVE_EXACT)
        self.assertEqual(outcome.depth, 0)
        # The transcript remains tracked until restore_live_session closes the
        # exact managed surface. A live-edge outcome is evidence, not cleanup.
        self.assertTrue(backend.scroll_view_active(record))

    def test_codex_scroll_revalidates_owner_and_sends_no_key_when_query_fails(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        backend._scroll_views[record.id] = state
        failed = subprocess.CompletedProcess(
            args=["tmux"], returncode=1, stdout="", stderr="query failed"
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=failed) as run:
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.disposition, ScrollDisposition.UNHANDLED)
        self.assertIs(outcome.edge, EdgeKnowledge.UNKNOWN)
        self.assertEqual(outcome.depth, 30)
        self.assertEqual(backend.scroll_state(record), state)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][0], "display-message")

    def test_codex_scroll_after_process_exit_never_sends_stale_transcript_key_to_shell(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        shell = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="0\t0\tbash\t0\t0\n", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=shell) as run:
            outcome = backend.scroll_session(record, 3)

        self.assertIs(outcome.disposition, ScrollDisposition.UNHANDLED)
        self.assertIs(outcome.owner, HistoryOwner.LIVE)
        self.assertFalse(backend.scroll_view_active(record))
        self.assertEqual(len(run.call_args_list), 1)
        self.assertNotIn("send-keys", run.call_args.args[0])

    def test_history_io_is_serialized_per_session_not_globally(self):
        first = _record(id="td-first", tmux_name="termdeck_first")
        second = _record(id="td-second", tmux_name="termdeck_second")
        backend = TmuxBackend(executable="tmux")
        now = time.monotonic()
        backend._scroll_views[first.id] = BackendHistoryState(
            owner=HistoryOwner.TMUX,
            depth=30,
            extent=120,
            command="bash",
            validated_at=now,
        )
        backend._scroll_views[second.id] = BackendHistoryState(
            owner=HistoryOwner.TMUX,
            depth=30,
            extent=120,
            command="bash",
            validated_at=now,
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        outcomes = {}

        def run(args, **_kwargs):
            joined = " ".join(args)
            if backend.pane_target(first) in joined:
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=2))
                return subprocess.CompletedProcess(
                    args=["tmux"], returncode=0, stdout="1\t27\t120\n", stderr=""
                )
            if backend.pane_target(second) in joined:
                second_finished.set()
                return subprocess.CompletedProcess(
                    args=["tmux"], returncode=0, stdout="1\t27\t120\n", stderr=""
                )
            raise AssertionError(f"unexpected tmux command: {args!r}")

        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=run):
            first_thread = threading.Thread(
                target=lambda: outcomes.setdefault("first", backend.scroll_session(first, 3))
            )
            second_thread = threading.Thread(
                target=lambda: outcomes.setdefault("second", backend.scroll_session(second, 3))
            )
            first_thread.start()
            self.assertTrue(first_entered.wait(timeout=1))
            second_thread.start()
            self.assertTrue(
                second_finished.wait(timeout=1),
                "second terminal was convoyed behind the first terminal's blocked I/O",
            )
            second_thread.join(timeout=1)
            self.assertFalse(second_thread.is_alive())
            release_first.set()
            first_thread.join(timeout=1)
            self.assertFalse(first_thread.is_alive())

        self.assertEqual(outcomes["first"].depth, 27)
        self.assertEqual(outcomes["second"].depth, 27)

    def test_scroll_mouse_reporting_alternate_screen_is_passed_through_without_copy_mode(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        for command_name in ("2.1.226", "grok", "unknown-tui"):
            for lines in (-3, 3):
                with self.subTest(command=command_name, lines=lines):
                    state = subprocess.CompletedProcess(
                        args=["tmux"],
                        returncode=0,
                        stdout=f"1\t0\t{command_name}\t1\t1\n",
                        stderr="",
                    )
                    with mock.patch(
                        "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
                    ), mock.patch.object(backend, "_run", return_value=state) as run:
                        outcome = backend.scroll_session(record, lines)

                    self.assertIs(outcome.disposition, ScrollDisposition.PASSTHROUGH)
                    self.assertIs(outcome.owner, HistoryOwner.CHILD_TUI)
                    self.assertIs(outcome.edge, EdgeKnowledge.UNKNOWN)
                    self.assertIs(
                        outcome.child_input_protocol,
                        ChildInputProtocol.SGR_MOUSE,
                    )
                    self.assertFalse(backend.scroll_view_active(record))
                    run.assert_called_once()

    def test_scroll_legacy_mouse_owner_reports_exact_child_protocol(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="1\t0\tlegacy-tui\t1\t0\n",
            stderr="",
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=state):
            outcome = backend.scroll_session(record, -3)

        self.assertIs(outcome.disposition, ScrollDisposition.PASSTHROUGH)
        self.assertIs(outcome.owner, HistoryOwner.CHILD_TUI)
        self.assertIs(
            outcome.child_input_protocol,
            ChildInputProtocol.LEGACY_MOUSE,
        )

    def test_scroll_no_mouse_alternate_screen_consumes_wheel_without_synthetic_keys(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="1\t0\tunknown-tui\t0\t0\n",
            stderr="",
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=state) as run:
            outcome = backend.scroll_session(record, -3)

        self.assertIs(outcome.disposition, ScrollDisposition.UNHANDLED)
        self.assertIs(outcome.owner, HistoryOwner.CHILD_TUI)
        self.assertIs(outcome.edge, EdgeKnowledge.UNKNOWN)
        self.assertIn("no mouse reporting", outcome.detail)
        self.assertFalse(backend.scroll_view_active(record))
        run.assert_called_once()

    def test_scroll_alternate_screen_repairs_stale_copy_mode_before_passthrough(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t1\t2.1.226\t1\t1\n", stderr=""
        )
        restored = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[state, restored]) as run:
            outcome = backend.scroll_session(record, -3)

        self.assertIs(outcome.disposition, ScrollDisposition.PASSTHROUGH)
        self.assertFalse(backend.scroll_view_active(record))
        self.assertEqual(
            run.call_args_list[-1],
            mock.call(
                ["copy-mode", "-q", "-t", backend.pane_target(record)],
                check=False,
                timeout=backend.query_timeout,
            ),
        )

    def test_large_codex_scrollbar_jump_uses_page_navigation(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=24,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[context, completed]) as run:
            outcome = backend.scroll_session(record, -96)

        self.assertIs(outcome.disposition, ScrollDisposition.HANDLED)
        self.assertEqual(outcome.depth, 120)
        self.assertEqual(
            run.call_args_list[-1],
            mock.call(
            [
                "send-keys",
                "-t",
                backend.pane_target(record),
                "-N",
                "4",
                "PageUp",
            ],
            check=False,
            timeout=backend.query_timeout,
            ),
        )

    def test_interactive_session_leaves_pointer_with_tui_and_bounds_history(self):
        record = _record()
        backend = TmuxBackend(executable="tmux", history_limit=2500)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.configure_interactive_session(record)

        run.assert_called_once_with(
            [
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-t",
                record.tmux_name,
                "history-limit",
                "2500",
                ";",
                "set-option",
                "-w",
                "-t",
                backend.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                backend.pane_target(record),
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_restore_live_session_disables_mouse_and_leaves_copy_mode(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            backend.restore_live_session(record)

        run.assert_called_once_with(
            [
                "set-option",
                "-t",
                record.tmux_name,
                "mouse",
                "off",
                ";",
                "set-option",
                "-w",
                "-t",
                backend.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                backend.pane_target(record),
            ],
            check=False,
            timeout=backend.query_timeout,
        )

    def test_restore_live_codex_transcript_closes_only_termdeck_managed_transcript(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr="")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", side_effect=[context, completed]
        ) as run:
            restored = backend.restore_live_session(record)

        self.assertTrue(restored)
        self.assertFalse(backend.scroll_view_active(record))
        self.assertEqual(run.call_args.args[0][:5], ["send-keys", "-t", backend.pane_target(record), "C-t", ";"])

    def test_restore_live_failure_preserves_tracked_history_state(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        backend._scroll_views[record.id] = state
        context = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="1\t0\tcodex\t0\t0\n", stderr=""
        )
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="failed")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", side_effect=[context, failed]
        ):
            restored = backend.restore_live_session(record)

        self.assertFalse(restored)
        self.assertEqual(backend.scroll_state(record), state)

    def test_restore_live_codex_owner_query_failure_blocks_input_and_keeps_state(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        state = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        backend._scroll_views[record.id] = state
        failed = subprocess.CompletedProcess(
            args=["tmux"], returncode=1, stdout="", stderr="query failed"
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", return_value=failed) as run:
            restored = backend.restore_live_session(record, force=True)

        self.assertFalse(restored)
        self.assertEqual(backend.scroll_state(record), state)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertNotIn("C-t", run.call_args.args[0])

    def test_restore_live_after_codex_exit_clears_stale_state_without_sending_ct(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.CODEX,
            depth=30,
            managed_open=True,
            command="codex",
            validated_at=time.monotonic(),
        )
        shell = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="0\t0\tbash\t0\t0\n", stderr=""
        )
        completed = subprocess.CompletedProcess(
            args=["tmux"], returncode=0, stdout="", stderr=""
        )
        with mock.patch(
            "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
        ), mock.patch.object(backend, "_run", side_effect=[shell, completed]) as run:
            restored = backend.restore_live_session(record, force=True)

        self.assertTrue(restored)
        self.assertFalse(backend.scroll_view_active(record))
        self.assertNotIn("C-t", run.call_args_list[-1].args[0])

    def test_send_text_restores_untracked_copy_mode_before_literal_text_and_enter(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "create_or_resume") as create, mock.patch.object(
            backend, "restore_live_session"
        ) as restore, mock.patch.object(backend, "_run") as run:
            backend.send_text(record, "captain's $literal; text", enter=True)

        create.assert_called_once_with(record)
        restore.assert_called_once_with(record, force=True)
        self.assertFalse(backend.scroll_view_active(record))
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "send-keys",
                        "-t",
                        backend.pane_target(record),
                        "-l",
                        "captain's $literal; text",
                    ]
                ),
                mock.call(["send-keys", "-t", backend.pane_target(record), "C-m"]),
            ],
        )

    def test_send_text_refuses_to_leak_input_when_live_restore_fails(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "create_or_resume"), mock.patch.object(
            backend, "restore_live_session", return_value=False
        ), mock.patch.object(backend, "_run") as run:
            with self.assertRaisesRegex(TmuxError, "restored from history"):
                backend.send_text(record, "must-not-leak", enter=True)

        run.assert_not_called()

    def test_terminate_forgets_tracked_scroll_view(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        backend._scroll_views[record.id] = BackendHistoryState(
            owner=HistoryOwner.TMUX,
            depth=3,
            extent=100,
            command="bash",
            validated_at=time.monotonic(),
        )
        with mock.patch.object(backend, "session_exists", return_value=False):
            backend.terminate(record)

        self.assertFalse(backend.scroll_view_active(record))

    def test_interactive_configuration_failure_backs_off(self):
        first = _record(id="first", tmux_name="first")
        second = _record(id="second", tmux_name="second")
        backend = TmuxBackend(executable="tmux", failure_backoff=60)
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=124, stdout="", stderr="timeout")
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ) as run:
            backend.configure_interactive_session(first)
            backend.configure_interactive_session(second)

        run.assert_called_once()

    def test_attach_command_shape(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        self.assertEqual(backend.attach_command(record), ["tmux", "attach-session", "-t", "termdeck_dev_test"])
        self.assertEqual(backend.pane_target(record), "termdeck_dev_test:0.0")

    def test_adopted_pane_target_overrides_default_target(self):
        record = _record(
            id="td-test",
            name="legacy",
            tag="adopted",
            tmux_name="legacy",
            pane_target="%7",
            external=True,
        )
        backend = TmuxBackend(executable="tmux")
        self.assertEqual(backend.pane_target(record), "%7")

    def test_managed_pane_target_ignores_stale_server_local_id(self):
        record = _record(
            id="td-managed",
            name="managed",
            tag="managed",
            tmux_name="termdeck_managed",
            pane_target="%161",
            external=False,
        )
        backend = TmuxBackend(executable="tmux")

        self.assertEqual(backend.pane_target(record), "termdeck_managed:0.0")

    def test_managed_stale_pane_id_resolves_from_recreated_session(self):
        managed = _record(
            id="managed",
            tmux_name="termdeck_managed",
            pane_target="%161",
            external=False,
        )
        adopted = _record(
            id="adopted",
            tmux_name="external_session",
            pane_target="%9",
            external=True,
        )
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout=(
                "termdeck_managed\t0\t0\t%3\t111\t/tmp\t0\n"
                "external_session\t1\t2\t%9\t222\t/tmp\t0\n"
            ),
            stderr="",
        )

        with mock.patch(
            "termdeck.tmux_backend.shutil.which",
            return_value="/usr/bin/tmux",
        ), mock.patch.object(backend, "_run", return_value=completed) as run:
            pids = backend.pane_pids([managed, adopted])

        self.assertEqual(pids, {"managed": 111, "adopted": 222})
        run.assert_called_once()

    def test_current_directory_reads_live_pane_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            record = _record()
            backend = TmuxBackend(executable="tmux")
            completed = subprocess.CompletedProcess(
                args=["tmux"],
                returncode=0,
                stdout=f"{tmp}\n",
                stderr="",
            )
            with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                result = backend.current_directory(record)

            self.assertEqual(result, str(Path(tmp).resolve()))
            run.assert_called_once_with(
                [
                    "display-message",
                    "-p",
                    "-t",
                    "termdeck_dev_test:0.0",
                    "#{pane_current_path}",
                ],
                check=False,
                timeout=backend.query_timeout,
            )

    def test_current_directories_share_batched_pane_snapshot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first_directory = Path(tmp) / "first"
            second_directory = Path(tmp) / "second"
            first_directory.mkdir()
            second_directory.mkdir()
            first = _record(id="a", tmux_name="sess_a")
            second = _record(id="b", tmux_name="sess_b", pane_target="%9", external=True)
            backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
            completed = subprocess.CompletedProcess(
                args=["tmux"],
                returncode=0,
                stdout=(
                    f"sess_a\t0\t0\t%3\t111\t{first_directory}\n"
                    f"sess_b\t1\t2\t%9\t222\t{second_directory}\n"
                ),
                stderr="",
            )
            with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
                backend, "_run", return_value=completed
            ) as run:
                directories = backend.current_directories([first, second])
                pids = backend.pane_pids([first, second])

            self.assertEqual(
                directories,
                {"a": str(first_directory.resolve()), "b": str(second_directory.resolve())},
            )
            self.assertEqual(pids, {"a": 111, "b": 222})
            run.assert_called_once()

    def test_session_exists_many_uses_one_bounded_list_call(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", query_timeout=0.2)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_b\nunrelated\n",
            stderr="",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=completed
        ) as run:
            result = backend.session_exists_many([first, second])

        self.assertEqual(result, {"a": False, "b": True})
        run.assert_called_once_with(
            ["list-sessions", "-F", "#{session_name}"],
            check=False,
            timeout=0.2,
        )

    def test_session_batch_timeout_preserves_prior_live_state(self):
        running = _record(id="live", status="running")
        stopped = _record(id="stopped", status="stopped")
        backend = TmuxBackend(executable="tmux")
        timed_out = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=124,
            stdout="",
            stderr="tmux command timed out",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=timed_out
        ):
            result = backend.session_exists_many([running, stopped])

        self.assertEqual(result, {"live": True, "stopped": False})

    def test_session_batch_missing_server_reports_every_session_gone(self):
        running = _record(id="live", status="running")
        stopped = _record(id="stopped", status="stopped")
        backend = TmuxBackend(executable="tmux")
        errors = (
            "error connecting to /run/user/1000/tmux-1000/default "
            "(No such file or directory)",
            "error connecting to /run/user/1000/tmux-1000/default "
            "(Connection refused)",
            "no server running on /run/user/1000/tmux-1000/default",
        )

        for stderr in errors:
            with self.subTest(stderr=stderr):
                no_server = subprocess.CompletedProcess(
                    args=["tmux"],
                    returncode=1,
                    stdout="",
                    stderr=stderr,
                )
                with mock.patch(
                    "termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"
                ), mock.patch.object(backend, "_run", return_value=no_server):
                    result = backend.session_exists_many([running, stopped])

                self.assertEqual(result, {"live": False, "stopped": False})

    def test_session_batch_failure_is_stale_not_dead(self):
        running = _record(id="live", status="running")
        stopped = _record(id="stopped", status="stopped")
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=1,
            stdout="",
            stderr="server busy",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ):
            result = backend.session_exists_many([running, stopped])

        self.assertEqual(result, {"live": True, "stopped": False})

    def test_pane_snapshot_timeout_does_not_expand_into_per_session_calls(self):
        first = _record(id="a", tmux_name="sess_a", last_pid=111)
        second = _record(id="b", tmux_name="sess_b", last_pid=222)
        backend = TmuxBackend(executable="tmux")
        timed_out = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=124,
            stdout="",
            stderr="tmux command timed out",
        )

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=timed_out
        ) as run:
            result = backend.pane_pids([first, second])

        self.assertEqual(result, {"a": 111, "b": 222})
        run.assert_called_once()

    def test_fleet_snapshot_failure_has_backoff_and_never_fans_out(self):
        records = [_record(id=f"td-{index}", tmux_name=f"sess-{index}", last_pid=index) for index in range(2000)]
        backend = TmuxBackend(executable="tmux", failure_backoff=60)
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="busy")

        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ) as run:
            first = backend.pane_pids(records)
            second = backend.pane_pids(records)

        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["td-1999"], 1999)
        self.assertEqual(second, first)
        self.assertTrue(backend.pane_snapshot_stale)
        self.assertGreater(backend._query_timeout_for(2000), backend.query_timeout)
        self.assertLessEqual(backend._query_timeout_for(2000), backend.command_timeout)

    def test_capture_is_one_bounded_tmux_call(self):
        record = _record()
        backend = TmuxBackend(executable="tmux", max_capture_lines=500)
        completed = subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="tail\n", stderr="")
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            captured = backend.capture(record, lines=5000)

        self.assertEqual(captured, "tail\n")
        run.assert_called_once_with(
            ["capture-pane", "-t", backend.pane_target(record), "-p", "-S", "-500"],
            check=False,
            timeout=backend._query_timeout_for(500),
        )

    def test_run_returns_bounded_failure_for_nonchecking_query(self):
        backend = TmuxBackend(executable="tmux", command_timeout=0.1)
        expired = subprocess.TimeoutExpired(["tmux", "list-sessions"], 0.1)
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
            "termdeck.tmux_backend.subprocess.run", side_effect=expired
        ) as run:
            result = backend._run(["list-sessions"], check=False)

        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.1)

    def test_run_raises_clear_error_for_timed_out_command(self):
        backend = TmuxBackend(executable="tmux", command_timeout=0.1)
        expired = subprocess.TimeoutExpired(["tmux", "new-session"], 0.1)
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch(
            "termdeck.tmux_backend.subprocess.run", side_effect=expired
        ):
            with self.assertRaisesRegex(TmuxError, "timed out after 0.1s"):
                backend._run(["new-session"])

    def test_current_directory_returns_none_when_tmux_path_is_unavailable(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=1,
            stdout="",
            stderr="missing pane",
        )
        with mock.patch("termdeck.tmux_backend.shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            backend, "_run", return_value=failed
        ):
            self.assertIsNone(backend.current_directory(record))

    def test_pane_pid_uses_single_list_panes_call(self):
        record = _record()
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="termdeck_dev_test\t0\t0\tpane\t4321\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pid(record), 4321)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "list-panes")

    def test_is_codex_running_accepts_shell_backed_live_codex_tree(self):
        record = _record(command="/bin/bash")
        backend = TmuxBackend(executable="tmux")
        with mock.patch.object(backend, "pane_pid", return_value=4321) as pane_pid, mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=True
        ) as contains_codex:
            self.assertTrue(backend.is_codex_running(record))

        pane_pid.assert_called_once_with(record)
        contains_codex.assert_called_once_with(4321)

    def test_is_codex_running_rejects_claude_and_grok_process_trees(self):
        backend = TmuxBackend(executable="tmux")
        for command in ("claude", "grok"):
            with self.subTest(command=command):
                record = _record(command=command)
                with mock.patch.object(backend, "pane_pid", return_value=4321), mock.patch(
                    "termdeck.tmux_backend.process_tree_contains_codex", return_value=False
                ):
                    self.assertFalse(backend.is_codex_running(record))

    def test_is_codex_running_fails_closed_for_missing_or_unreadable_process_tree(self):
        record = _record(command="codex --yolo")
        backend = TmuxBackend(executable="tmux")

        with mock.patch.object(backend, "pane_pid", return_value=None), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex"
        ) as contains_codex:
            self.assertFalse(backend.is_codex_running(record))
        contains_codex.assert_not_called()

        with mock.patch.object(backend, "pane_pid", return_value=4321), mock.patch(
            "termdeck.tmux_backend.process_tree_contains_codex", return_value=None
        ):
            self.assertFalse(backend.is_codex_running(record))

        with mock.patch.object(backend, "pane_pid", side_effect=RuntimeError("lookup failed")):
            self.assertFalse(backend.is_codex_running(record))

    def test_pane_pid_reuses_shared_all_pane_cache(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\tpane-a\t111\nsess_b\t0\t0\tpane-b\t222\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pid(first), 111)
            self.assertEqual(backend.pane_pid(second), 222)
        run.assert_called_once()

    def test_pane_pids_batch_maps_default_and_percent_targets(self):
        default = _record(id="a", tmux_name="sess_a")
        adopted = _record(id="b", tmux_name="sess_b", pane_target="%9", external=True)
        backend = TmuxBackend(executable="tmux")
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout=(
                "sess_a\t0\t0\t%3\t111\n"
                "sess_b\t1\t2\t%9\t222\n"
            ),
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed):
            pids = backend.pane_pids([default, adopted])
        self.assertEqual(pids["a"], 111)
        self.assertEqual(pids["b"], 222)

    def test_pane_pids_falls_back_per_session_when_list_all_fails(self):
        record = _record(id="only")
        backend = TmuxBackend(executable="tmux")
        failed = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="no server")
        with mock.patch.object(backend, "_run", return_value=failed):
            with mock.patch.object(backend, "pane_pid", return_value=999) as single:
                pids = backend.pane_pids([record])
        single.assert_called_once_with(record)
        self.assertEqual(pids["only"], 999)

    def test_pane_pids_uses_short_lived_cache(self):
        first = _record(id="a", tmux_name="sess_a")
        second = _record(id="b", tmux_name="sess_b")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        completed = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout=(
                "sess_a\t0\t0\t%3\t111\n"
                "sess_b\t0\t0\t%4\t222\n"
            ),
            stderr="",
        )
        with mock.patch.object(backend, "_run", return_value=completed) as run:
            self.assertEqual(backend.pane_pids([first])["a"], 111)
            self.assertEqual(backend.pane_pids([second])["b"], 222)
        run.assert_called_once()

    def test_invalidate_cache_forces_next_tmux_refresh(self):
        record = _record(id="a", tmux_name="sess_a")
        backend = TmuxBackend(executable="tmux", pane_cache_ttl=60)
        first = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\t%3\t111\n",
            stderr="",
        )
        second = subprocess.CompletedProcess(
            args=["tmux"],
            returncode=0,
            stdout="sess_a\t0\t0\t%3\t222\n",
            stderr="",
        )
        with mock.patch.object(backend, "_run", side_effect=[first, second]) as run:
            self.assertEqual(backend.pane_pids([record])["a"], 111)
            backend.invalidate_cache()
            self.assertEqual(backend.pane_pids([record])["a"], 222)
        self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()
