from __future__ import annotations

import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Sequence

from .checkpointing import CheckpointResult, DmtcpCheckpointManager
from .models import SessionRecord
from .resources import observe_recovery_metadata, process_tree_contains_codex, safe_codex_recovery_command


class TmuxError(RuntimeError):
    pass


class ScrollDisposition(Enum):
    """Describe who must handle one terminal scroll gesture."""

    UNHANDLED = "unhandled"
    HANDLED = "handled"
    PASSTHROUGH = "passthrough"

    def __bool__(self) -> bool:
        return self is ScrollDisposition.HANDLED


@dataclass(frozen=True)
class AuthActivationResult:
    """Result of making a private auth-file change effective in a live tab."""

    ok: bool
    restarted: bool
    recovery_command: str | None = None
    recovery_cwd: str | None = None
    pid: int | None = None
    status: str | None = None
    detail: str = ""


class TmuxBackend:
    """Hidden tmux backend.

    The GUI calls these methods; users never see or type tmux commands.
    """

    _READ_ONLY_CONTROL_COMMANDS = frozenset(
        {
            "capture-pane",
            "display-message",
            "has-session",
            "list-panes",
            "list-sessions",
            "show-environment",
        }
    )

    def __init__(
        self,
        executable: str = "tmux",
        *,
        pane_cache_ttl: float = 1.0,
        command_timeout: float = 2.0,
        query_timeout: float = 0.35,
        failure_backoff: float = 1.0,
        history_limit: int = 2000,
        max_capture_lines: int = 10000,
        recovery_scan_limit: int = 128,
        checkpoint_manager: DmtcpCheckpointManager | None = None,
    ) -> None:
        self.executable = executable
        self.pane_cache_ttl = max(0.0, float(pane_cache_ttl))
        self.command_timeout = max(0.1, float(command_timeout))
        self.query_timeout = max(0.05, float(query_timeout))
        self.failure_backoff = max(0.05, float(failure_backoff))
        self.history_limit = max(100, int(history_limit))
        self.max_capture_lines = max(100, int(max_capture_lines))
        self.recovery_scan_limit = max(1, int(recovery_scan_limit))
        self.checkpoints = checkpoint_manager or DmtcpCheckpointManager()
        self._recovery_scan_cursor = 0
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target: dict[str, int] = {}
        self._pane_cache_by_session_active: dict[str, int] = {}
        self._pane_cwd_by_target: dict[str, str] = {}
        self._pane_cwd_by_session_active: dict[str, str] = {}
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0
        self._interactive_config_retry_at = 0.0
        self._query_backoff_until = 0.0
        self._scroll_views: dict[str, str] = {}

    def invalidate_cache(self) -> None:
        """Drop cached tmux pane metadata after structural changes."""
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target = {}
        self._pane_cache_by_session_active = {}
        self._pane_cwd_by_target = {}
        self._pane_cwd_by_session_active = {}
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0

    def _query_timeout_for(self, item_count: int) -> float:
        """Give large fleet snapshots enough time without allowing hangs."""

        count = max(0, int(item_count))
        extra = max(0, count - 64) * 0.00075
        ceiling = max(self.query_timeout, self.command_timeout)
        return min(ceiling, self.query_timeout + extra)

    @property
    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    @property
    def pane_snapshot_stale(self) -> bool:
        """Whether pane PID results are last-known data after a failed query."""

        return self._pane_snapshot_stale

    def require_available(self) -> None:
        if not self.available:
            raise TmuxError("tmux is not installed or not found on PATH")

    def _run(
        self,
        args: Sequence[str],
        check: bool = True,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.require_available()
        command = [self.executable, *args]
        limit = self.command_timeout if timeout is None else max(0.05, float(timeout))
        command_name = str(args[0]) if args else ""
        read_only_control = command_name in self._READ_ONLY_CONTROL_COMMANDS
        if read_only_control and time.monotonic() < self._query_backoff_until:
            detail = f"tmux query suppressed during control-plane backoff: {command_name or 'command'}"
            if check:
                raise TmuxError(detail)
            return subprocess.CompletedProcess(command, 124, "", detail)
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=limit,
            )
        except subprocess.TimeoutExpired as exc:
            detail = f"tmux command timed out after {limit:g}s: {args[0] if args else 'command'}"
            if read_only_control:
                self._query_backoff_until = time.monotonic() + self.failure_backoff
            if check:
                raise TmuxError(detail) from exc
            return subprocess.CompletedProcess(command, 124, "", detail)
        if check and result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux command failed")
        return result

    def session_exists(self, record: SessionRecord) -> bool:
        if not self.available:
            return False
        result = self._run(
            ["has-session", "-t", record.tmux_name],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode == 124:
            # A transiently busy tmux server is not proof that a remembered
            # running session disappeared. Preserve the prior live state and
            # let the next bounded query repair it.
            return record.status.casefold() in {"running", "paused", "missing"}
        return result.returncode == 0

    def private_auth_environment_ready(self, record: SessionRecord) -> bool:
        """Verify that a live tmux session was born with this tab's auth scope.

        Assigning ``codex_home`` to an old SQLite row cannot change an already
        running shell's environment. Account switching must fail closed for
        those legacy sessions, otherwise ``codex-auth`` can resolve to the
        global executable and mutate another tab's credentials.
        """

        if getattr(record, "external", False) or not self.available:
            return False
        raw_home = (getattr(record, "codex_home", None) or "").strip()
        scope_id = (getattr(record, "auth_scope_id", None) or "").strip()
        if not raw_home or not scope_id:
            return False
        expected_home = str(Path(raw_home).expanduser().resolve(strict=False))
        expected_bin = str(Path(expected_home) / "bin")
        result = self._run(
            ["show-environment", "-t", record.tmux_name],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return False
        environment: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line or line.startswith("-") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            environment[key] = value
        path_entries = environment.get("PATH", "").split(os.pathsep)
        return (
            environment.get("CODEX_HOME") == expected_home
            and environment.get("TERMDECK_AUTH_SCOPE_ID") == scope_id
            and bool(path_entries)
            and path_entries[0] == expected_bin
        )

    @staticmethod
    def _server_is_absent(result: subprocess.CompletedProcess[str]) -> bool:
        detail = str(result.stderr or "").casefold()
        return "no server running on" in detail or (
            ("no such file or directory" in detail or "connection refused" in detail)
            and ("error connecting to" in detail or "connect failed" in detail)
        )

    def session_exists_many(self, records: Sequence[SessionRecord]) -> dict[str, bool]:
        """Resolve many sessions with one bounded tmux query."""

        out = {record.id: False for record in records}
        if not records or not self.available:
            return out
        result = self._run(
            ["list-sessions", "-F", "#{session_name}"],
            check=False,
            timeout=self._query_timeout_for(len(records)),
        )
        if result.returncode != 0:
            if self._server_is_absent(result):
                # This is the normal post-reboot state, not a busy control
                # plane. Report the sessions missing so startup recovery can
                # relaunch every remembered managed tab from durable records.
                return out
            # A failed control-plane snapshot is not proof that thousands of
            # independent sessions died. Keep the last known lifecycle state;
            # a later successful bounded query will reconcile it.
            return {
                record.id: record.status.casefold() in {"running", "paused", "missing"}
                for record in records
            }
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return {record.id: record.tmux_name in names for record in records}

    @staticmethod
    def _resume_uuid(command: str) -> str | None:
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if "resume" not in parts:
            return None
        index = parts.index("resume")
        if index + 2 != len(parts):
            return None
        candidate = parts[index + 1].strip()
        return candidate or None

    @staticmethod
    def _state_db_columns(connection: sqlite3.Connection, table: str) -> list[str]:
        return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]

    @staticmethod
    def _canonical_state_db_candidates(private_db: Path) -> list[Path]:
        candidates: list[Path] = []
        configured = os.environ.get("TERMDECK_CODEX_STATE_SOURCE", "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.append(Path.home() / ".codex" / "state_5.sqlite")
        runtime_root = Path("/var/lib/codex-runtime")
        try:
            runtime_homes = sorted(
                (entry for entry in runtime_root.iterdir() if entry.is_dir()),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError:
            runtime_homes = []
        candidates.extend(entry / "state_5.sqlite" for entry in runtime_homes)

        private_resolved = private_db.expanduser().resolve(strict=False)
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            resolved = candidate.expanduser().resolve(strict=False)
            key = str(resolved)
            if resolved == private_resolved or key in seen:
                continue
            seen.add(key)
            unique.append(resolved)
        return unique

    def _hydrate_private_codex_recovery(self, record: SessionRecord, recovery: str) -> None:
        """Hydrate one exact thread row into a private Codex state database.

        Per-terminal auth homes intentionally keep SQLite state separate to avoid
        multi-terminal lock contention. Exact recovery still needs the target
        thread metadata, so copy only that row (plus direct FK/tool metadata)
        from a canonical state DB immediately before launching ``codex resume``.
        """

        raw_home = (getattr(record, "codex_home", None) or "").strip()
        thread_id = self._resume_uuid(recovery)
        if not raw_home or not thread_id:
            return
        private_db = Path(raw_home).expanduser().resolve(strict=False) / "state_5.sqlite"
        if not private_db.is_file():
            # A brand-new private home lets Codex create/backfill its own DB from
            # the shared rollout tree. Hydration matters once a private DB exists.
            return

        try:
            local = sqlite3.connect(private_db, timeout=5.0)
        except sqlite3.Error as exc:
            raise TmuxError(f"could not open private Codex state database for recovery: {exc}") from exc
        local.row_factory = sqlite3.Row
        try:
            if "threads" not in {
                str(row[0])
                for row in local.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }:
                raise TmuxError("private Codex state database has no threads table")
            if local.execute("SELECT 1 FROM threads WHERE id=? LIMIT 1", (thread_id,)).fetchone():
                return
            local_thread_columns = self._state_db_columns(local, "threads")

            source_connection: sqlite3.Connection | None = None
            source_row: sqlite3.Row | None = None
            source_path: Path | None = None
            for candidate in self._canonical_state_db_candidates(private_db):
                if not candidate.is_file():
                    continue
                try:
                    source = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True, timeout=5.0)
                    source.row_factory = sqlite3.Row
                except sqlite3.Error:
                    continue
                try:
                    if self._state_db_columns(source, "threads") != local_thread_columns:
                        source.close()
                        continue
                    row = source.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
                    if row is None:
                        source.close()
                        continue
                    source_connection = source
                    source_row = row
                    source_path = candidate
                    break
                except sqlite3.Error:
                    source.close()
                    continue

            if source_connection is None or source_row is None:
                raise TmuxError(
                    f"exact Codex session metadata is absent from the private and canonical state databases: {thread_id}"
                )

            try:
                local.execute("PRAGMA foreign_keys=ON")
                local.execute("BEGIN IMMEDIATE")
                section_id = source_row["thread_section_id"] if "thread_section_id" in source_row.keys() else None
                if section_id:
                    source_section_columns = self._state_db_columns(source_connection, "thread_sections")
                    local_section_columns = self._state_db_columns(local, "thread_sections")
                    if source_section_columns != local_section_columns:
                        raise TmuxError("Codex thread_sections schema mismatch during exact recovery")
                    section = source_connection.execute(
                        "SELECT * FROM thread_sections WHERE id=?", (section_id,)
                    ).fetchone()
                    if section is None:
                        raise TmuxError("Codex recovery thread references a missing thread section")
                    quoted = ",".join(f'"{column}"' for column in local_section_columns)
                    placeholders = ",".join("?" for _ in local_section_columns)
                    local.execute(
                        f"INSERT OR IGNORE INTO thread_sections ({quoted}) VALUES ({placeholders})",
                        [section[column] for column in local_section_columns],
                    )

                quoted = ",".join(f'"{column}"' for column in local_thread_columns)
                placeholders = ",".join("?" for _ in local_thread_columns)
                local.execute(
                    f"INSERT INTO threads ({quoted}) VALUES ({placeholders})",
                    [source_row[column] for column in local_thread_columns],
                )

                source_tool_columns = self._state_db_columns(source_connection, "thread_dynamic_tools")
                local_tool_columns = self._state_db_columns(local, "thread_dynamic_tools")
                if source_tool_columns and source_tool_columns == local_tool_columns:
                    tools = source_connection.execute(
                        "SELECT * FROM thread_dynamic_tools WHERE thread_id=? ORDER BY position",
                        (thread_id,),
                    ).fetchall()
                    if tools:
                        quoted_tools = ",".join(f'"{column}"' for column in local_tool_columns)
                        placeholders_tools = ",".join("?" for _ in local_tool_columns)
                        local.executemany(
                            f"INSERT OR IGNORE INTO thread_dynamic_tools ({quoted_tools}) VALUES ({placeholders_tools})",
                            ([tool[column] for column in local_tool_columns] for tool in tools),
                        )
                local.commit()
            except Exception:
                local.rollback()
                raise
            finally:
                source_connection.close()

            if not local.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
                raise TmuxError(
                    f"Codex recovery metadata hydration did not persist thread {thread_id} from {source_path}"
                )
        except TmuxError:
            raise
        except sqlite3.Error as exc:
            raise TmuxError(f"could not hydrate exact Codex recovery metadata: {exc}") from exc
        finally:
            local.close()

    def create_or_resume(self, record: SessionRecord) -> None:
        if self.session_exists(record):
            return
        if getattr(record, "external", False):
            raise TmuxError(f"adopted tmux session not found: {record.tmux_name}")
        launch_directory = Path(record.cwd).expanduser()
        launch_directory.mkdir(parents=True, exist_ok=True)
        original_command = record.command.strip() or os.environ.get("SHELL", "/bin/sh")
        command = original_command
        recovery_pending = record.status.casefold() == "recovery-pending"
        # A lost process may resume only an exact UUID-bound Codex session.
        # Launcher-only snapshots remain useful for observation/UI but are never
        # executable recovery state.
        recovery = self._exact_codex_recovery(record)
        if recovery:
            self._hydrate_private_codex_recovery(record, recovery)
            raw_recovery_cwd = (getattr(record, "recovery_cwd", None) or "").strip()
            if raw_recovery_cwd:
                recovery_directory = Path(raw_recovery_cwd).expanduser()
                if recovery_directory.is_absolute() and recovery_directory.is_dir():
                    try:
                        launch_directory = recovery_directory.resolve()
                    except OSError:
                        launch_directory = recovery_directory
            # A shell-backed tab should remain useful after the recovered TUI
            # exits. Only append an exact, argument-free known shell; malformed
            # or non-shell launch metadata cannot become shell syntax here.
            try:
                launch_argv = shlex.split(original_command)
            except ValueError:
                launch_argv = []
            shell_names = {
                "bash",
                "csh",
                "dash",
                "elvish",
                "fish",
                "ksh",
                "nu",
                "sh",
                "tcsh",
                "zsh",
            }
            if len(launch_argv) == 1 and Path(launch_argv[0]).name.casefold() in shell_names:
                command = f"{recovery}; exec {shlex.join(launch_argv)}"
            else:
                command = recovery
            # Codex owns durable conversation state and uses SIGUSR2 itself.
            # Never place either exact Codex recovery or its trailing shell under
            # DMTCP: doing so steals signal 12 from Codex and can prevent startup.
        else:
            checkpoint_command = self.checkpoints.restart_command(record)
            if checkpoint_command:
                launch_directory = self.checkpoints.session_dir(record)
                # Exact recovery is fail-closed. A stale or incompatible image
                # must remain an explicit failed restore; replaying the launch
                # command would create a different process state.
                command = checkpoint_command
            elif recovery_pending:
                raise TmuxError(
                    "exact process recovery is unavailable; no Codex session UUID "
                    "or validated process checkpoint was saved"
                )
            else:
                command = (
                    original_command
                    if self._is_codex_launch_command(original_command)
                    else self.checkpoints.wrap_launch_command(record, original_command)
                )
        # tmux accepts the command as one shell command argument, which preserves
        # normal shell syntax for commands such as "npm run dev" or "codex".
        launch_args = [
            "new-session",
            "-d",
            "-s",
            record.tmux_name,
        ]
        raw_codex_home = (getattr(record, "codex_home", None) or "").strip()
        if raw_codex_home:
            codex_home = Path(raw_codex_home).expanduser().resolve(strict=False)
            host_path = os.environ.get("PATH", "")
            isolated_path = f"{codex_home / 'bin'}:{host_path}" if host_path else str(codex_home / "bin")
            launch_args.extend(
                [
                    "-e",
                    f"CODEX_HOME={codex_home}",
                    "-e",
                    f"TERMDECK_AUTH_SCOPE_ID={getattr(record, 'auth_scope_id', '') or ''}",
                    "-e",
                    f"PATH={isolated_path}",
                ]
            )
        launch_args.extend([
            "-c",
            str(launch_directory),
            command,
        ])
        self._run(launch_args)
        # Make the session visually clean when attached.
        self._run(["set-option", "-t", record.tmux_name, "status", "off"], check=False)
        self._run(["set-option", "-t", record.tmux_name, "remain-on-exit", "on"], check=False)
        self.invalidate_cache()

    def attach_command(self, record: SessionRecord) -> list[str]:
        return [self.executable, "attach-session", "-t", record.tmux_name]

    def configure_interactive_session(self, record: SessionRecord) -> None:
        """Keep pointer input application-owned with bounded pane history.

        TermDeck and VTE own scrolling and focus. Enabling tmux mouse capture
        makes an ordinary wheel gesture enter copy mode, where subsequent keys
        move a history cursor instead of reaching a full-screen TUI composer.
        Explicitly disable that capture while retaining a finite history limit.
        Failure is non-fatal: attaching still works.
        """

        if not self.available:
            return
        self._scroll_views.pop(record.id, None)
        now = time.monotonic()
        if now < self._interactive_config_retry_at:
            return
        result = self._run(
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
                str(self.history_limit),
                ";",
                "set-option",
                "-w",
                "-t",
                self.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                self.pane_target(record),
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            self._interactive_config_retry_at = now + self.failure_backoff

    def scroll_view_active(self, record: SessionRecord) -> bool:
        return record.id in self._scroll_views

    def scroll_session(self, record: SessionRecord, lines: int) -> ScrollDisposition:
        """Route scrolling without leaking synthetic arrows into a TUI composer."""

        amount = int(lines)
        if not amount or not self.available:
            return ScrollDisposition.UNHANDLED
        target = self.pane_target(record)
        view = self._scroll_views.get(record.id)
        if view is None:
            # Scrolling down at the live bottom is a no-op and must not open a
            # history surface.
            if amount > 0:
                return ScrollDisposition.UNHANDLED
            current = self._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    target,
                    "#{alternate_on}\t#{pane_in_mode}\t#{pane_current_command}",
                ],
                check=False,
                timeout=self.query_timeout,
            )
            if current.returncode != 0:
                return ScrollDisposition.UNHANDLED
            fields = current.stdout.rstrip("\n").split("\t", 2)
            if len(fields) != 3:
                return ScrollDisposition.UNHANDLED
            alternate_on = fields[0] == "1"
            pane_in_mode = fields[1] == "1"
            command = Path(fields[2].strip()).name.casefold()
            if command == "codex" or command.startswith("codex-"):
                view = "codex-transcript"
            elif alternate_on:
                # Full-screen applications own their virtual scrollback. tmux
                # copy mode can only expose the handful of lines emitted before
                # the alternate screen opened, which freezes a clipped snapshot
                # for Claude Code, Grok, and other TUIs. Leave stale copy mode
                # first, then let VTE deliver this wheel event to the child.
                if pane_in_mode:
                    restored = self._run(
                        ["copy-mode", "-q", "-t", target],
                        check=False,
                        timeout=self.query_timeout,
                    )
                    if restored.returncode != 0:
                        return ScrollDisposition.UNHANDLED
                self._scroll_views.pop(record.id, None)
                return ScrollDisposition.PASSTHROUGH
            else:
                view = "tmux-history"

        if view == "codex-transcript":
            repetitions = max(1, abs(amount))
            key = "Up" if amount < 0 else "Down"
            if repetitions > 24:
                # A scrollbar drag can jump hundreds of history lines. Codex's
                # transcript pager accepts page keys, avoiding a flood of one
                # arrow sequence per line while wheel-sized deltas stay precise.
                repetitions = (repetitions + 23) // 24
                key = "PageUp" if amount < 0 else "PageDown"
            count = str(repetitions)
            keys = []
            if record.id not in self._scroll_views:
                # Codex's alternate screen has no emulator scrollback. Open its
                # transcript before navigation so Up never reaches the composer.
                keys = ["send-keys", "-t", target, "C-t", ";"]
            keys.extend(
                [
                    "send-keys",
                    "-t",
                    target,
                    "-N",
                    count,
                    key,
                ]
            )
        else:
            count = str(max(1, abs(amount)))
            keys = []
            if record.id not in self._scroll_views:
                keys = ["copy-mode", "-t", target, ";"]
            keys.extend(
                [
                    "send-keys",
                    "-X",
                    "-t",
                    target,
                    "-N",
                    count,
                    "scroll-up" if amount < 0 else "scroll-down",
                ]
            )
        result = self._run(
            keys,
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return ScrollDisposition.UNHANDLED
        self._scroll_views[record.id] = view
        return ScrollDisposition.HANDLED

    def restore_live_session(self, record: SessionRecord, *, force: bool = False) -> None:
        """Return a focused pane from historical copy mode to its live TUI."""

        if not self.available:
            return
        now = time.monotonic()
        if not force and now < self._interactive_config_retry_at:
            return
        view = self._scroll_views.pop(record.id, None)
        commands = []
        if view == "codex-transcript":
            commands = ["send-keys", "-t", self.pane_target(record), "C-t", ";"]
        commands.extend(
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
                self.pane_target(record),
                "window-size",
                "latest",
                ";",
                "copy-mode",
                "-q",
                "-t",
                self.pane_target(record),
            ]
        )
        result = self._run(
            commands,
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            self._interactive_config_retry_at = now + self.failure_backoff

    def pane_target(self, record: SessionRecord) -> str:
        explicit = getattr(record, "pane_target", None)
        return explicit or f"{record.tmux_name}:0.0"

    def current_directory(self, record: SessionRecord) -> str | None:
        """Return the active pane's live working directory when tmux can report it."""

        if not self.available:
            return None
        result = self._run(
            [
                "display-message",
                "-p",
                "-t",
                self.pane_target(record),
                "#{pane_current_path}",
            ],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        candidate = result.stdout.strip()
        return self._normalise_pane_directory(candidate)

    @staticmethod
    def _normalise_pane_directory(candidate: str) -> str | None:
        if not candidate:
            return None
        directory = Path(candidate).expanduser()
        if not directory.is_dir():
            return None
        try:
            directory = directory.resolve()
        except OSError:
            directory = directory.absolute()
        return str(directory)

    def current_directories(self, records: Sequence[SessionRecord]) -> dict[str, str | None]:
        """Resolve live pane directories for many sessions from one cached snapshot."""

        out: dict[str, str | None] = {record.id: None for record in records}
        if not records or not self.available:
            return out
        if self._load_pane_maps(len(records)) is None:
            return out
        for record in records:
            target = self.pane_target(record)
            candidate = self._pane_cwd_by_target.get(target)
            if candidate is None and target == f"{record.tmux_name}:0.0":
                candidate = self._pane_cwd_by_session_active.get(record.tmux_name)
            out[record.id] = self._normalise_pane_directory(candidate or "")
        return out

    def pane_pid(self, record: SessionRecord) -> int | None:
        """Return a pane pid while sharing one cached all-pane snapshot."""

        if not self.available:
            return None
        maps = self._load_pane_maps()
        if maps is not None:
            by_target, by_session_active = maps
            target = self.pane_target(record)
            if target in by_target:
                return by_target[target]
            if target == f"{record.tmux_name}:0.0":
                return by_session_active.get(record.tmux_name)
            return None
        if self._pane_snapshot_timed_out:
            return record.last_pid

        result = self._run(
            ["list-panes", "-t", self.pane_target(record), "-F", "#{pane_pid}\t#{pane_dead}"],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        first = result.stdout.strip().splitlines()[0:1]
        if not first:
            return None
        pid_text, _separator, dead_text = first[0].partition("\t")
        if dead_text.strip() not in {"", "0"}:
            return None
        try:
            return int(pid_text)
        except ValueError:
            return None

    def is_codex_running(self, record: SessionRecord) -> bool:
        """Return true only when the live pane process tree proves Codex is running."""

        try:
            pane_pid = self.pane_pid(record)
            if pane_pid is None:
                return False
            return process_tree_contains_codex(pane_pid) is True
        except (OSError, RuntimeError, ValueError):
            # Process inspection is necessarily racy. A vanished or unreadable
            # tree is not sufficient evidence to appoint this tab as a captain.
            return False

    def _load_pane_maps(
        self,
        record_count: int = 1,
    ) -> tuple[dict[str, int], dict[str, int]] | None:
        """Return cached tmux pane maps, refreshing with one list-panes call.

        UI surfaces can refresh together; without this cache each window/pane
        asks tmux for the same pane table and the app feels sluggish.  The cache
        is deliberately short-lived so process controls remain fresh enough for
        interactive use while avoiding repeated subprocess spawns per repaint.
        """

        now = time.monotonic()
        if self.pane_cache_ttl and now < self._pane_cache_expires_at:
            return self._pane_cache_by_target, self._pane_cache_by_session_active
        if now < self._pane_snapshot_retry_at:
            return None

        self._pane_snapshot_timed_out = False
        result = self._run(
            [
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_dead}",
            ],
            check=False,
            timeout=self._query_timeout_for(record_count),
        )
        if result.returncode != 0:
            self._pane_cache_expires_at = 0.0
            self._pane_cache_by_target = {}
            self._pane_cache_by_session_active = {}
            self._pane_cwd_by_target = {}
            self._pane_cwd_by_session_active = {}
            self._pane_snapshot_timed_out = result.returncode == 124
            self._pane_snapshot_stale = True
            self._pane_snapshot_retry_at = now + self.failure_backoff
            return None

        by_target: dict[str, int] = {}
        by_session_active: dict[str, int] = {}
        cwd_by_target: dict[str, str] = {}
        cwd_by_session_active: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t", 6)
            if len(parts) not in {5, 6, 7}:
                continue
            session_name, window_index, pane_index, pane_id, pane_pid = parts[:5]
            pane_cwd = parts[5] if len(parts) >= 6 else ""
            pane_dead = parts[6] if len(parts) == 7 else "0"
            if pane_dead.strip() not in {"", "0"}:
                continue
            try:
                pid = int(pane_pid)
            except ValueError:
                continue
            target = f"{session_name}:{window_index}.{pane_index}"
            by_target[target] = pid
            if pane_cwd:
                cwd_by_target[target] = pane_cwd
            if pane_id:
                pane_key = pane_id if pane_id.startswith("%") else f"%{pane_id}"
                by_target[pane_key] = pid
                if pane_cwd:
                    cwd_by_target[pane_key] = pane_cwd
            by_session_active.setdefault(session_name, pid)
            if pane_cwd:
                cwd_by_session_active.setdefault(session_name, pane_cwd)

        self._pane_cache_by_target = by_target
        self._pane_cache_by_session_active = by_session_active
        self._pane_cwd_by_target = cwd_by_target
        self._pane_cwd_by_session_active = cwd_by_session_active
        self._pane_cache_expires_at = now + self.pane_cache_ttl if self.pane_cache_ttl else 0.0
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0
        return by_target, by_session_active

    def pane_pids(self, records: Sequence[SessionRecord]) -> dict[str, int | None]:
        """Resolve pane pids for many sessions using a short-lived tmux snapshot.

        Supports default ``session:win.pane`` targets and adopted ``%pane_id`` targets.
        """

        out: dict[str, int | None] = {record.id: None for record in records}
        if not records or not self.available:
            return out

        maps = self._load_pane_maps(len(records))
        if maps is None:
            if self._pane_snapshot_timed_out or len(records) > 32:
                # Do not turn one bounded all-pane timeout into N additional
                # synchronous tmux calls. This also applies to a non-timeout
                # failure at fleet scale; recent PIDs are a safer stale view.
                return {record.id: record.last_pid for record in records}
            # Fall back to per-session lookups (same results as before).
            for record in records:
                out[record.id] = self.pane_pid(record)
            return out

        by_target, by_session_active = maps
        for record in records:
            target = self.pane_target(record)
            if target in by_target:
                out[record.id] = by_target[target]
            elif target == f"{record.tmux_name}:0.0":
                out[record.id] = by_session_active.get(record.tmux_name)
            else:
                out[record.id] = None
        return out

    def recovery_metadata(
        self,
        records: Sequence[SessionRecord],
        *,
        bounded: bool = True,
    ) -> dict[str, tuple[str | None, str | None]]:
        """Observe safe crash-relaunch commands and cwd values together.

        Periodic UI refreshes use a bounded round-robin scan. Shutdown and
        explicit checkpoint operations set ``bounded=False`` so every live tab
        gets one final exact-session observation before durable state is saved.
        """

        candidates = [record for record in records if not getattr(record, "external", False)]
        if not candidates:
            return {}
        pids = self.pane_pids(candidates)
        if self._pane_snapshot_stale:
            return {}
        live = [record for record in candidates if pids.get(record.id)]
        if not live:
            return {}
        if bounded:
            start = self._recovery_scan_cursor % len(live)
            count = min(len(live), self.recovery_scan_limit)
            selected = [live[(start + offset) % len(live)] for offset in range(count)]
            self._recovery_scan_cursor = (start + count) % len(live)
        else:
            selected = live
        out: dict[str, tuple[str | None, str | None]] = {}
        for record in selected:
            pid = pids.get(record.id)
            assert pid is not None
            observed, command, cwd = observe_recovery_metadata(pid)
            if observed:
                out[record.id] = (command, cwd if command else None)
        return out

    def recovery_commands(self, records: Sequence[SessionRecord]) -> dict[str, str | None]:
        """Return command-only recovery observations for older callers."""

        return {
            session_id: command
            for session_id, (command, _cwd) in self.recovery_metadata(records).items()
        }

    def activate_auth_change(self, record: SessionRecord) -> AuthActivationResult:
        """Make a selected private Codex account effective for this tab now.

        Changing ``auth.json`` cannot change credentials already loaded by a
        running Codex process. When Codex is live, capture its exact resumable
        session first, recreate only this tab's tmux session, then resume the
        same Codex conversation under the tab's private ``CODEX_HOME``. An
        idle shell does not need recreation: its next ``codex`` launch will
        read the newly selected private credentials.
        """

        if getattr(record, "external", False):
            return AuthActivationResult(False, False, detail="external tmux sessions cannot be recreated safely")
        if not self.session_exists(record):
            return AuthActivationResult(
                True,
                False,
                detail="terminal is stopped; the next Codex launch will use the selected account",
            )

        metadata = self.recovery_metadata([record], bounded=False)
        recovery_command, recovery_cwd = metadata.get(record.id, (None, None))
        if recovery_command:
            candidate = replace(
                record,
                recovery_command=recovery_command,
                recovery_cwd=recovery_cwd,
            )
            if self._exact_codex_recovery(candidate) is None:
                recovery_command = None
                recovery_cwd = None
        if not recovery_command:
            pane_pid = self.pane_pid(record)
            if pane_pid is None:
                return AuthActivationResult(
                    False,
                    False,
                    detail="could not inspect the live process tree before activating the new Codex account",
                )
            codex_state = process_tree_contains_codex(pane_pid)
            current = self._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    self.pane_target(record),
                    "#{pane_current_command}",
                ],
                check=False,
                timeout=self.query_timeout,
            )
            if current.returncode != 0:
                return AuthActivationResult(
                    False,
                    False,
                    detail="could not verify the live pane before activating the new Codex account",
                )
            current_name = Path(current.stdout.strip()).name.casefold()
            if codex_state is not False or current_name == "codex" or current_name.startswith("codex-"):
                return AuthActivationResult(
                    False,
                    False,
                    detail="running Codex session could not be captured exactly; exit Codex once and relaunch it",
                )
            return AuthActivationResult(
                True,
                False,
                detail="no running Codex process; the next Codex launch will use the selected account",
            )

        resumed = replace(
            record,
            recovery_command=recovery_command,
            recovery_cwd=recovery_cwd,
            status="running",
        )
        self.terminate(record)
        try:
            self.create_or_resume(resumed)
            if not self.private_auth_environment_ready(resumed):
                self.terminate(resumed)
                return AuthActivationResult(
                    False,
                    False,
                    recovery_command=recovery_command,
                    recovery_cwd=recovery_cwd,
                    status="recovery-pending",
                    detail="recreated tab did not verify its private Codex auth environment",
                )
            pid = self.pane_pid(resumed)
        except (OSError, TmuxError) as exc:
            return AuthActivationResult(
                False,
                False,
                recovery_command=recovery_command,
                recovery_cwd=recovery_cwd,
                status="recovery-pending",
                detail=f"could not resume Codex after the account change: {exc}",
            )
        return AuthActivationResult(
            True,
            True,
            recovery_command=recovery_command,
            recovery_cwd=recovery_cwd,
            pid=pid,
            status="running",
            detail="Codex restarted in this tab with the selected account",
        )

    @staticmethod
    def _is_codex_launch_command(command: str) -> bool:
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return safe_codex_recovery_command(argv) is not None

    @staticmethod
    def _exact_codex_recovery(record: SessionRecord) -> str | None:
        raw = (getattr(record, "recovery_command", None) or "").strip()
        if not raw:
            return None
        try:
            command = safe_codex_recovery_command(shlex.split(raw))
        except ValueError:
            return None
        if command is None:
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        return command if "resume" in parts else None

    def checkpoint_records(
        self,
        records: Sequence[SessionRecord],
    ) -> list[CheckpointResult]:
        """Persist exact restart state for every selected live managed tab."""

        rows = list(records)
        live_by_id = self.session_exists_many(rows)
        results: list[CheckpointResult] = []
        for record in rows:
            if getattr(record, "external", False):
                results.append(
                    CheckpointResult(
                        record.id,
                        "checkpoint",
                        False,
                        False,
                        "external tmux sessions cannot be checkpointed",
                    )
                )
                continue
            if not live_by_id.get(record.id, False):
                results.append(
                    CheckpointResult(
                        record.id,
                        "checkpoint",
                        False,
                        False,
                        "managed tmux session is not running",
                    )
                )
                continue
            exact_codex = self._exact_codex_recovery(record)
            if exact_codex:
                results.append(
                    CheckpointResult(
                        record.id,
                        "codex-resume",
                        True,
                        True,
                        "exact Codex session UUID persisted",
                    )
                )
                continue
            results.append(self.checkpoints.checkpoint(record))
        return results

    def send_text(self, record: SessionRecord, text: str, enter: bool = True) -> None:
        self.create_or_resume(record)
        # Exit copy-mode / Codex transcript view before sending so keys reach
        # the live composer rather than feeding the history cursor. This must
        # run even when tmux entered copy mode outside TermDeck, in which case
        # there is deliberately no entry in ``_scroll_views``.
        self.restore_live_session(record, force=True)
        if text:
            self._run(["send-keys", "-t", self.pane_target(record), "-l", text])
        if enter:
            self._run(["send-keys", "-t", self.pane_target(record), "C-m"])

    def capture(self, record: SessionRecord, lines: int = 200) -> str:
        clean_lines = min(self.max_capture_lines, max(1, int(lines)))
        start = f"-{clean_lines}"
        # capture-pane already reports a missing target. Avoid a redundant
        # has-session subprocess, especially for fleet-wide activity queries.
        result = self._run(
            ["capture-pane", "-t", self.pane_target(record), "-p", "-S", start],
            check=False,
            timeout=self._query_timeout_for(clean_lines),
        )
        return result.stdout if result.returncode == 0 else ""

    def pause(self, record: SessionRecord) -> int | None:
        pid = self.pane_pid(record)
        if not pid:
            return None
        self._signal_process_group(pid, signal.SIGSTOP)
        return pid

    def resume(self, record: SessionRecord) -> int | None:
        pid = self.pane_pid(record)
        if not pid:
            return None
        self._signal_process_group(pid, signal.SIGCONT)
        return pid

    def terminate(self, record: SessionRecord) -> None:
        self._scroll_views.pop(record.id, None)
        if self.session_exists(record):
            self._run(["kill-session", "-t", record.tmux_name], check=False)
        # A deliberate Stop/Kill must not resurrect stale process memory on the
        # next TermDeck launch. Browser-style Hide leaves the checkpoint intact.
        self.checkpoints.clear(record)
        self.invalidate_cache()

    def rename(self, record: SessionRecord, new_name: str) -> None:
        if self.session_exists(record):
            self._run(["rename-session", "-t", record.tmux_name, new_name], check=False)
        self.invalidate_cache()

    @staticmethod
    def _signal_process_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise TmuxError(f"permission denied while signalling process group for pid {pid}") from exc
        except OSError:
            # Some shells/programs may not be in a process group accessible this
            # way. Fall back to the direct pane PID.
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return


    def list_session_names(self) -> list[str]:
        """Return live tmux session names for diagnostics and adoption previews."""

        result = self._run(["list-sessions", "-F", "#{session_name}"], check=False)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux list-sessions failed")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_panes_text(self, format_string: str) -> str:
        result = self._run(["list-panes", "-a", "-F", format_string], check=False)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux list-panes failed")
        return result.stdout

    def debug_command(self, args: Sequence[str]) -> str:
        return " ".join(shlex.quote(part) for part in [self.executable, *args])
