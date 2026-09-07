#!/usr/bin/env python3
"""Verify window restore with exact Codex and an injected recovery backend."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from smoke_support import require_private_validation_runtime, stream_ready, stream_snapshot


_FIXTURE_TIMEOUT_SECONDS = 120.0
_RESTORE_TIMEOUT_SECONDS = 240.0


def _drain_gtk(
    Gtk,
    *,
    until=None,
    timeout: float = _RESTORE_TIMEOUT_SECONDS,
    detail: str = "GTK state did not settle",
    diagnostic=None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    suffix = ""
    if diagnostic is not None:
        try:
            suffix = f": {diagnostic()}"
        except Exception as exc:
            suffix = f": diagnostic failed: {exc}"
    raise RuntimeError(f"{detail} before timeout{suffix}")


def _wait_until(
    predicate,
    *,
    detail: str,
    timeout: float = _FIXTURE_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(detail)


def _pane_state(backend, record) -> dict[str, object]:
    line = backend._run(
        [
            "display-message",
            "-p",
            "-t",
            backend.pane_target(record),
            "#{pid}\t#{session_id}\t#{session_created}\t#{pane_id}\t"
            "#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}",
        ],
        timeout=10.0,
    ).stdout.rstrip("\n")
    parts = line.split("\t", 6)
    if len(parts) != 7:
        raise RuntimeError(f"tmux returned malformed pane state: {line!r}")
    server_pid, session_id, session_created, pane_id, pane_pid, dead, status = parts
    return {
        "server_pid": int(server_pid),
        "session_id": session_id,
        "session_created": session_created,
        "pane_id": pane_id,
        "pid": int(pane_pid),
        "dead": dead == "1",
        "status": int(status) if status.strip() else None,
    }


def _all_tabs_ready(app, expected_ids: set[str]) -> bool:
    tabs = [tab for window in app.windows for tab in window.tabs.values()]
    return (
        {tab.record.id for tab in tabs} == expected_ids
        and all(stream_ready(tab.widget) for tab in tabs)
    )


def _restore_diagnostic(app, coordinator) -> str:
    tabs = []
    for window in app.windows:
        for tab in window.tabs.values():
            snapshot = stream_snapshot(tab.widget)
            tabs.append(
                {
                    "generation": None if snapshot is None else snapshot.generation,
                    "id": tab.record.id,
                    "ready": False if snapshot is None else snapshot.ready,
                    "size": None if snapshot is None else snapshot.size,
                    "state": None if snapshot is None else str(snapshot.state),
                    "stopped": None if snapshot is None else snapshot.stopped,
                    "window_id": window.window_id,
                }
            )
    rows = [
        {
            "attempts": record.recovery_attempts,
            "error": record.recovery_last_error,
            "id": record.id,
            "last_pid": record.last_pid,
            "status": record.status,
            "tmux_name": record.tmux_name,
        }
        for record in app.store.list()
    ]
    pane_result = app.backend._run(
        [
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}",
        ],
        check=False,
        timeout=30.0,
        bypass_query_backoff=True,
    )
    return json.dumps(
        {
            "coordinator_inflight": sorted(coordinator.inflight_ids),
            "panes": pane_result.stdout.splitlines(),
            "restore_inflight": sorted(
                {
                    session_id
                    for window in app.windows
                    for session_id in window._restore_inflight_ids
                }
            ),
            "restore_retry": sorted(
                {
                    session_id
                    for window in app.windows
                    for session_id in window._restore_retry_ids
                }
            ),
            "rows": rows,
            "tabs": tabs,
            "windows": len(app.windows),
        },
        sort_keys=True,
    )


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _wait_for_attached_client(
    tmux_name: str,
    process: subprocess.Popen[str],
    log_path: Path,
    *,
    timeout: float = _FIXTURE_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(
                f"secondary database GUI exited before attaching {tmux_name}: {detail}"
            )
        result = subprocess.run(
            ["tmux", "list-clients", "-F", "#{session_name}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if tmux_name in result.stdout.splitlines():
            return
        time.sleep(0.05)
    detail = log_path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(
        f"secondary database GUI did not attach {tmux_name}: {detail}"
    )


def _write_private_recovery_state(
    path: Path,
    *,
    thread_id: str,
    rollout_path: Path,
    cwd: Path,
) -> None:
    """Create the bounded native-state precondition for exact private recovery.

    Production refuses to run ``codex resume`` in a private CODEX_HOME unless
    its SQLx ledger and backfill marker are complete. The smoke must satisfy
    that boundary explicitly rather than relying on an empty test directory or
    mocking the hardened recovery path away.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path)
    try:
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
            ) VALUES (1, 'complete', 'sessions/last.jsonl', 1234, 1234)
            """
        )
        connection.execute(
            "INSERT INTO thread_sections(id, name) VALUES (?, 'Pinned')",
            ("01984de2-8f74-7c91-a3b2-5c5e937cf318",),
        )
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd) VALUES (?, ?, ?)",
            (thread_id, str(rollout_path), str(cwd)),
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _install_fixture_launcher_attestation(launcher: Path) -> None:
    from termdeck import codex_config_scope

    expected = launcher.resolve(strict=True)

    def attest_fixture(candidate: Path) -> bool:
        return candidate == expected

    prepare = codex_config_scope.prepare_shared_codex_configuration
    migrate = codex_config_scope.migrate_quiescent_codex_configuration

    def prepare_fixture(private_home, canonical_home):
        return prepare(
            private_home,
            canonical_home,
            launcher_attestor=attest_fixture,
        )

    def migrate_fixture(private_home):
        return migrate(private_home, launcher_attestor=attest_fixture)

    codex_config_scope.prepare_shared_codex_configuration = prepare_fixture
    codex_config_scope.migrate_quiescent_codex_configuration = migrate_fixture


def main() -> int:
    # Keep TMUX_TMPDIR comfortably below the Unix-domain socket path limit,
    # including when CI places TMPDIR under a deep checkout path.
    runtime_base = require_private_validation_runtime()
    with tempfile.TemporaryDirectory(prefix="td-", dir=runtime_base) as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "m"
        fake_bin = root / "bin"
        recovery_cwd = root / "recovered-repo"
        recovery_marker = root / "recovered-command.txt"
        dead_marker = root / "dead-once.started"
        dead_release = root / "dead-once.release"
        codex_session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        exact_recovery_command = f"codex --yolo resume {codex_session_id}"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        fake_bin.mkdir()
        recovery_cwd.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n%s\\n' \"$*\" \"$PWD\" > \"$TD_RECOVERY_MARKER\"\n"
            "exec -a codex /bin/sleep 86400\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        dead_once = fake_bin / "dead-once"
        dead_once.write_text(
            "#!/bin/sh\n"
            "if [ ! -e \"$TD_DEAD_MARKER\" ]; then\n"
            "  : > \"$TD_DEAD_MARKER\"\n"
            "  while [ ! -e \"$TD_DEAD_RELEASE\" ]; do sleep 0.05; done\n"
            "  exit 73\n"
            "fi\n"
            "exec \"${SHELL:-/bin/sh}\"\n",
            encoding="utf-8",
        )
        dead_once.chmod(0o755)
        managed_root = root / "managed"
        managed_root.mkdir(mode=0o700)
        canonical_home = home / ".codex"
        canonical_home.mkdir(mode=0o700)
        canonical_accounts = canonical_home / "accounts"
        canonical_accounts.mkdir(mode=0o700)
        for account_id in ("alpha", "beta"):
            account_path = canonical_accounts / f"{account_id}.json"
            account_path.write_text(json.dumps({"fixture": account_id}) + "\n")
            account_path.chmod(0o600)
        (canonical_home / "auth.json").symlink_to(Path("accounts") / "alpha.json")
        canonical_current = canonical_home / "current"
        canonical_current.write_text("alpha\n")
        canonical_current.chmod(0o600)
        for directory in ("sessions", "archived_sessions", "prompts"):
            (canonical_home / directory).mkdir(mode=0o700)
        canonical_rollout = (
            canonical_home
            / "sessions"
            / time.strftime("%Y/%m/%d", time.gmtime())
            / f"rollout-smoke-{codex_session_id}.jsonl"
        )
        canonical_rollout.parent.mkdir(parents=True, exist_ok=True)
        canonical_rollout.write_text(
            json.dumps(
                {
                    "payload": {
                        "id": codex_session_id,
                        "cwd": str(recovery_cwd),
                        "source": "cli",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        canonical_rollout.chmod(0o600)
        _write_private_recovery_state(
            canonical_home / "state_5.sqlite",
            thread_id=codex_session_id,
            rollout_path=canonical_rollout,
            cwd=recovery_cwd,
        )
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["TERMDECK_TMUX_RUNTIME"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["TD_RECOVERY_MARKER"] = str(recovery_marker)
        os.environ["TD_DEAD_MARKER"] = str(dead_marker)
        os.environ["TD_DEAD_RELEASE"] = str(dead_release)
        os.environ["T1000_MANAGED_ROOT"] = str(managed_root)
        os.environ["CODEX_STORAGE_FABRIC"] = "direct-root"
        os.environ["CODEX_WORKSPACE_STORAGE_CLASS"] = "non-rotational-block"
        os.environ["CODEX_STORAGE_ATTESTATION"] = "window-restore-private-ssd"
        os.environ["TERMDECK_CODEX_BINARY"] = str(fake_codex)
        os.environ["TERMDECK_CODEX_ACCOUNT_SOURCE"] = str(canonical_home)
        _install_fixture_launcher_attestation(fake_codex)

        from termdeck.checkpointing import CheckpointResult
        from termdeck.codex_auth import prepare_auth_scope
        from termdeck.gtk_app import (
            Gtk,
            TermDeckApplication,
            _tabs_pane_records,
            application_id_for_database,
        )
        from termdeck.session_store import DEFAULT_DB_PATH, SessionStore
        from termdeck.tmux_backend import TmuxBackend

        class SmokeCheckpointManager:
            """Simulate an explicitly injected validated-image recovery backend."""

            def __init__(self, directory: Path) -> None:
                self.directory = directory

            def restart_command(self, record) -> str:
                return record.command

            def session_dir(self, record) -> Path:
                path = self.directory / record.id
                path.mkdir(parents=True, exist_ok=True)
                return path

            @staticmethod
            def wrap_launch_command(_record, command: str) -> str:
                return command

            @staticmethod
            def checkpoint(record) -> CheckpointResult:
                return CheckpointResult(
                    record.id,
                    "checkpoint",
                    True,
                    True,
                    "simulated validated checkpoint",
                    1,
                )

            @staticmethod
            def clear(_record) -> None:
                return None

        app = TermDeckApplication(db_path=root / "sessions.sqlite3")
        app.register(None)
        app.backend.checkpoints = SmokeCheckpointManager(root / "checkpoints")
        secondary_db = root / "secondary.sqlite3"
        secondary_store = SessionStore(secondary_db)
        secondary_backend = TmuxBackend()
        secondary_process: subprocess.Popen[str] | None = None
        secondary_log = root / "secondary-gui.log"
        secondary_log_handle = None
        records = []
        secondary_records = []
        default_sentinels: dict[Path, tuple[bytes, tuple[int, ...]]] = {}
        try:
            for sentinel_path in {
                DEFAULT_DB_PATH,
                home / ".local" / "share" / "termdeck" / "sessions.sqlite3",
            }:
                sentinel_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                sentinel = f"window-restore-default-db:{sentinel_path}\n".encode()
                sentinel_path.write_bytes(sentinel)
                sentinel_path.chmod(0o600)
                default_sentinels[sentinel_path] = (
                    sentinel,
                    _file_identity(sentinel_path),
                )

            # Persist the exact state left by a host crash: the registry says
            # these tabs were running, but the tmux server and processes are
            # gone. Ten windows with three tabs each prove both that startup
            # restores the complete window catalog and that it does not defer
            # terminals at the former 24-terminal attachment limit.
            for window_index in range(1, 11):
                for tab_index in range(3):
                    is_dead_fixture = window_index == 1 and tab_index == 1
                    record = app.store.create(
                        name=f"agent-{window_index}-{tab_index}",
                        tag="terminal",
                        command=(
                            str(dead_once)
                            if is_dead_fixture
                            else os.environ.get("SHELL", "/bin/sh")
                        ),
                        cwd=str(root),
                        environment=(
                            {
                                "TD_DEAD_MARKER": str(dead_marker),
                                "TD_DEAD_RELEASE": str(dead_release),
                            }
                            if is_dead_fixture
                            else None
                        ),
                        window_id=f"window-{window_index}",
                        window_title=f"Window {window_index}",
                        tab_index=tab_index,
                        visible=True,
                    )
                    records.append(
                        app.store.update(
                            record.id,
                            status="running",
                            recovery_intent="active",
                            last_pid=90_000 + len(records),
                        )
                    )

            # Generic terminal records do not create Codex state. This one
            # smoke row explicitly opts into the optional private-account
            # adapter because it is about exact Codex recovery.
            legacy_scope_id = "auth-window-restore-legacy"
            private_home = root / "legacy-auth" / legacy_scope_id
            prepare_auth_scope(private_home, account_source=canonical_home)
            private_rollout = canonical_rollout
            private_state = private_home / "state_5.sqlite"
            private_state.unlink(missing_ok=True)
            private_state.with_name(f"{private_state.name}-wal").unlink(missing_ok=True)
            private_state.with_name(f"{private_state.name}-shm").unlink(missing_ok=True)
            _write_private_recovery_state(
                private_state,
                thread_id=codex_session_id,
                rollout_path=private_rollout,
                cwd=recovery_cwd,
            )
            legacy_sentinel = private_home / "legacy-scope.sentinel"
            legacy_sentinel.write_text("legacy scope must remain unchanged\n")
            legacy_sentinel.chmod(0o600)
            legacy_sentinel_state = (
                legacy_sentinel.read_bytes(),
                _file_identity(legacy_sentinel),
            )
            app.store.conn.execute(
                """
                UPDATE sessions
                SET auth_scope_id = ?, codex_home = ?, selected_account = ?
                WHERE id = ?
                """,
                (legacy_scope_id, str(private_home), "alpha", records[0].id),
            )
            app.store.conn.commit()
            # Its separately persisted recovery command must run before the
            # recreated tab returns to that shell.
            records[0] = app.store.update(
                records[0].id,
                recovery_command=exact_recovery_command,
                recovery_cwd=str(recovery_cwd),
                last_opened_at=records[0].last_opened_at,
            )

            app.backend.create_or_resume(records[1])
            _wait_until(
                dead_marker.exists,
                detail="dead-pane fixture did not start",
            )
            dead_before = _pane_state(app.backend, records[1])
            records[1] = app.store.update(
                records[1].id,
                last_pid=int(dead_before["pid"]),
            )
            dead_release.touch(mode=0o600)
            _wait_until(
                lambda: bool(_pane_state(app.backend, records[1])["dead"]),
                detail="dead-pane fixture did not become a retained dead pane",
            )
            dead_snapshot = _pane_state(app.backend, records[1])
            for field in ("server_pid", "session_id", "session_created", "pane_id"):
                if dead_snapshot[field] != dead_before[field]:
                    raise AssertionError(
                        f"dead-pane fixture changed {field} before recovery"
                    )

            secondary = secondary_store.create(
                name="secondary-agent",
                tag="terminal",
                command=os.environ.get("SHELL", "/bin/sh"),
                cwd=str(root),
                window_id="secondary-window",
                window_title="Secondary registry",
                visible=True,
            )
            secondary_backend.create_or_resume(secondary)
            secondary_records.append(
                secondary_store.update(
                    secondary.id,
                    status="running",
                    last_pid=secondary_backend.pane_pid(secondary),
                )
            )

            secondary_application_id = application_id_for_database(secondary_db)
            if app.get_application_id() == secondary_application_id:
                raise AssertionError("separate database files shared one GApplication identity")
            if app.get_is_remote():
                raise AssertionError("primary database GUI unexpectedly registered as remote")
            app.activate()
            coordinator = app._get_recovery_coordinator()
            expected_primary_ids = {record.id for record in records}
            secondary_env = os.environ.copy()
            for name in ("T1000_MANAGED_ROOT", "TERMDECK_TMUX_RUNTIME", "TMUX"):
                secondary_env.pop(name, None)
            repo_root = Path(__file__).resolve().parents[1]
            secondary_env["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (
                    str(repo_root),
                    secondary_env.get("PYTHONPATH", ""),
                )
                if value
            )
            secondary_log_handle = secondary_log.open("w", encoding="utf-8")
            secondary_process = subprocess.Popen(
                [
                    os.environ.get("PYTHON", sys.executable),
                    "-m",
                    "termdeck.cli",
                    "gui",
                    "--db",
                    str(secondary_db),
                ],
                cwd=root,
                env=secondary_env,
                text=True,
                stdout=secondary_log_handle,
                stderr=subprocess.STDOUT,
            )
            _wait_for_attached_client(
                secondary_records[0].tmux_name,
                secondary_process,
                secondary_log,
            )
            _drain_gtk(
                Gtk,
                until=lambda: (
                    len(app.windows) == 10
                    and sum(len(window.tabs) for window in app.windows) == 30
                    and recovery_marker.exists()
                    and _all_tabs_ready(app, expected_primary_ids)
                    and not coordinator.inflight_ids
                ),
                detail="startup did not restore every remembered pane stream",
                diagnostic=lambda: _restore_diagnostic(app, coordinator),
            )

            titles = {window.window_title for window in app.windows}
            expected = {f"Window {index}" for index in range(1, 11)}
            if titles != expected:
                raise AssertionError(
                    f"relaunch omitted remembered windows: expected={sorted(expected)}, "
                    f"actual={sorted(titles)}"
                )

            attached = sum(len(window.tabs) for window in app.windows)
            if attached != len(records):
                raise AssertionError(
                    "startup did not attach every remembered tab: "
                    f"attached={attached}, remembered={len(records)}"
                )
            refreshed = app.store.list()
            if {record.id for record in refreshed} != expected_primary_ids:
                raise AssertionError("primary database session identity set changed")
            if len(refreshed) != len(records) or not all(record.visible for record in refreshed):
                raise AssertionError("crash recovery hid or dropped remembered tabs")
            restarted = [record for record in refreshed if app.backend.session_exists(record)]
            deferred = [record for record in refreshed if not app.backend.session_exists(record)]
            if len(restarted) != len(records) or deferred:
                raise AssertionError(
                    "crash recovery did not restart every remembered tab: "
                    f"restarted={len(restarted)}, deferred={len(deferred)}"
                )
            recovered = recovery_marker.read_text(encoding="utf-8").splitlines()
            expected_recovery_argv = (
                f"--dangerously-bypass-hook-trust --yolo resume {codex_session_id}"
            )
            recovered_record = app.store.get(records[0].id)
            if recovered_record is None:
                raise AssertionError("exact recovery row disappeared")
            managed_codex_home = Path((recovered_record.codex_home or "").strip())
            if not managed_codex_home.is_absolute() or managed_codex_home == private_home:
                raise AssertionError(
                    "exact recovery did not rebind into a distinct managed Codex scope: "
                    f"home={managed_codex_home} old={private_home}"
                )
            expected_managed_home = app.store.auth_root / legacy_scope_id
            if managed_codex_home != expected_managed_home:
                raise AssertionError(
                    "exact recovery used the wrong managed auth scope: "
                    f"expected={expected_managed_home} actual={managed_codex_home}"
                )
            if (
                legacy_sentinel.read_bytes(),
                _file_identity(legacy_sentinel),
            ) != legacy_sentinel_state:
                raise AssertionError("legacy auth scope was mutated during rebind")
            expected_managed_argv = (
                f"-c sqlite_home={json.dumps(str(managed_codex_home))} "
                f"{expected_recovery_argv}"
            )
            if recovered != [expected_managed_argv, str(recovery_cwd)]:
                raise AssertionError(
                    "saved foreground Codex command or cwd was not restored: "
                    f"actual={recovered!r}"
                )
            dead_after = _pane_state(app.backend, records[1])
            if dead_after["dead"]:
                raise AssertionError("dead pane remained dead after startup recovery")
            for field in ("server_pid", "session_id", "session_created", "pane_id"):
                if dead_after[field] != dead_snapshot[field]:
                    raise AssertionError(
                        f"dead-pane recovery changed durable {field} identity"
                    )
            if dead_after["pid"] == dead_snapshot["pid"]:
                raise AssertionError("dead-pane recovery retained the exited process id")
            for window in app.windows:
                owned = [record for record in records if record.window_id == window.window_id]
                pane_records = _tabs_pane_records(app.store, window.window_id)
                if len(owned) != 3 or {record.id for record in pane_records} != {
                    record.id for record in owned
                }:
                    raise AssertionError(
                        f"remembered tabs disappeared from {window.window_title}"
                    )

            # Simulate a partial single-instance activation: eight GTK windows
            # remain realized while two disappear. Reactivation must fill only
            # the missing catalog IDs instead of treating a non-empty
            # ``app.windows`` list as proof that restore is complete.
            removed_ids = {window.window_id for window in app.windows[-2:]}
            for window in app.windows[-2:]:
                window.destroy()
            _drain_gtk(
                Gtk,
                until=lambda: len(app.windows) == 8,
                detail="destroyed windows did not leave the active catalog",
            )
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: (
                    len(app.windows) == 10
                    and sum(len(window.tabs) for window in app.windows) == 30
                    and _all_tabs_ready(app, expected_primary_ids)
                    and not coordinator.inflight_ids
                ),
                detail="reactivation did not restore the partial window catalog",
                diagnostic=lambda: _restore_diagnostic(app, coordinator),
            )
            restored_ids = {window.window_id for window in app.windows}
            if restored_ids != {f"window-{index}" for index in range(1, 11)}:
                raise AssertionError(
                    "reactivation did not reconstruct the complete unique window set: "
                    f"actual={sorted(restored_ids)}"
                )
            if not removed_ids.issubset(restored_ids):
                raise AssertionError("reactivation omitted one of the two removed windows")

            quit_secondary = subprocess.run(
                [
                    "gapplication",
                    "action",
                    secondary_application_id,
                    "quit-preserving-sessions",
                ],
                cwd=root,
                env=secondary_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if quit_secondary.returncode != 0:
                raise AssertionError(
                    "secondary GUI did not accept graceful shutdown: "
                    f"{quit_secondary.stderr.strip()}"
                )
            secondary_process.wait(timeout=60)
            if secondary_process.returncode != 0:
                raise AssertionError(
                    f"secondary GUI exited with {secondary_process.returncode}"
                )
            secondary_ids = {record.id for record in secondary_store.list()}
            expected_secondary_ids = {record.id for record in secondary_records}
            if secondary_ids != expected_secondary_ids:
                raise AssertionError("secondary database session identity set changed")
            for sentinel_path, sentinel_state in default_sentinels.items():
                if (
                    sentinel_path.read_bytes(),
                    _file_identity(sentinel_path),
                ) != sentinel_state:
                    raise AssertionError(
                        f"explicit database startup mutated default DB {sentinel_path}"
                    )

            print(
                json.dumps(
                    {
                        "all_remembered_windows_restored": sorted(titles),
                        "attached_at_startup": attached,
                        "all_pane_streams_ready": True,
                        "all_tabs_preserved": len(refreshed),
                        "dead_pane_recovered_in_place": {
                            "pane_id": dead_after["pane_id"],
                            "previous_pid": dead_snapshot["pid"],
                            "recovered_pid": dead_after["pid"],
                            "session_id": dead_after["session_id"],
                        },
                        "default_databases_untouched": sorted(
                            str(path) for path in default_sentinels
                        ),
                        "foreground_command_relaunched": exact_recovery_command,
                        "foreground_command_cwd": str(recovery_cwd),
                        "ok": True,
                        "partially_missing_windows_reconstructed": sorted(removed_ids),
                        "restarted_after_crash": len(restarted),
                        "session_ids": {
                            "primary": sorted(expected_primary_ids),
                            "secondary": sorted(expected_secondary_ids),
                        },
                        "separate_database_instances": [
                            app.get_application_id(),
                            secondary_application_id,
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for record in records:
                try:
                    if app.backend.session_exists(record):
                        app.backend.terminate(record)
                except Exception:
                    pass
            if secondary_process is not None and secondary_process.poll() is None:
                subprocess.run(
                    [
                        "gapplication",
                        "action",
                        secondary_application_id,
                        "quit-preserving-sessions",
                    ],
                    cwd=root,
                    env=secondary_env,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                try:
                    secondary_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            if secondary_log_handle is not None:
                secondary_log_handle.close()
            for record in secondary_records:
                try:
                    if secondary_backend.session_exists(record):
                        secondary_backend.terminate(record)
                except Exception:
                    pass
            for window in list(Gtk.Window.list_toplevels()):
                try:
                    window.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()
            secondary_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
