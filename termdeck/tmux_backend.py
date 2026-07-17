from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .models import SessionRecord


class TmuxError(RuntimeError):
    pass


class TmuxBackend:
    """Hidden tmux backend.

    The GUI calls these methods; users never see or type tmux commands.
    """

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
    ) -> None:
        self.executable = executable
        self.pane_cache_ttl = max(0.0, float(pane_cache_ttl))
        self.command_timeout = max(0.1, float(command_timeout))
        self.query_timeout = max(0.05, float(query_timeout))
        self.failure_backoff = max(0.05, float(failure_backoff))
        self.history_limit = max(100, int(history_limit))
        self.max_capture_lines = max(100, int(max_capture_lines))
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target: dict[str, int] = {}
        self._pane_cache_by_session_active: dict[str, int] = {}
        self._pane_cwd_by_target: dict[str, str] = {}
        self._pane_cwd_by_session_active: dict[str, str] = {}
        self._pane_snapshot_timed_out = False
        self._pane_snapshot_stale = False
        self._pane_snapshot_retry_at = 0.0
        self._interactive_config_retry_at = 0.0
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
            # A failed control-plane snapshot is not proof that thousands of
            # independent sessions died. Keep the last known lifecycle state;
            # a later successful bounded query will reconcile it.
            return {
                record.id: record.status.casefold() in {"running", "paused", "missing"}
                for record in records
            }
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return {record.id: record.tmux_name in names for record in records}

    def create_or_resume(self, record: SessionRecord) -> None:
        if self.session_exists(record):
            return
        if getattr(record, "external", False):
            raise TmuxError(f"adopted tmux session not found: {record.tmux_name}")
        Path(record.cwd).expanduser().mkdir(parents=True, exist_ok=True)
        command = record.command.strip() or os.environ.get("SHELL", "/bin/sh")
        # tmux accepts the command as one shell command argument, which preserves
        # normal shell syntax for commands such as "npm run dev" or "codex".
        self._run([
            "new-session",
            "-d",
            "-s",
            record.tmux_name,
            "-c",
            str(Path(record.cwd).expanduser()),
            command,
        ])
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

    def scroll_session(self, record: SessionRecord, lines: int) -> bool:
        """Scroll tmux history or Codex's transcript without typing in its composer."""

        amount = int(lines)
        if not amount or not self.available:
            return False
        target = self.pane_target(record)
        view = self._scroll_views.get(record.id)
        if view is None:
            # Scrolling down at the live bottom is a no-op and must not open a
            # history surface.
            if amount > 0:
                return False
            current = self._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    target,
                    "#{pane_current_command}",
                ],
                check=False,
                timeout=self.query_timeout,
            )
            if current.returncode != 0:
                return False
            command = Path(current.stdout.strip()).name.casefold()
            view = (
                "codex-transcript"
                if command == "codex" or command.startswith("codex-")
                else "tmux-history"
            )

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
            return False
        self._scroll_views[record.id] = view
        return True

    def restore_live_session(self, record: SessionRecord) -> None:
        """Return a focused pane from historical copy mode to its live TUI."""

        if not self.available:
            return
        now = time.monotonic()
        if now < self._interactive_config_retry_at:
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
            ["list-panes", "-t", self.pane_target(record), "-F", "#{pane_pid}"],
            check=False,
            timeout=self.query_timeout,
        )
        if result.returncode != 0:
            return None
        first = result.stdout.strip().splitlines()[0:1]
        if not first:
            return None
        try:
            return int(first[0])
        except ValueError:
            return None

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
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}",
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
            parts = line.split("\t", 5)
            if len(parts) not in {5, 6}:
                continue
            session_name, window_index, pane_index, pane_id, pane_pid = parts[:5]
            pane_cwd = parts[5] if len(parts) == 6 else ""
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

    def send_text(self, record: SessionRecord, text: str, enter: bool = True) -> None:
        self.create_or_resume(record)
        # Exit copy-mode / Codex transcript view before sending so keys reach
        # the live composer rather than feeding the history cursor.
        if self.scroll_view_active(record):
            self.restore_live_session(record)
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
