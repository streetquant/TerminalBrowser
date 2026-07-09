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

    def __init__(self, executable: str = "tmux", *, pane_cache_ttl: float = 1.0) -> None:
        self.executable = executable
        self.pane_cache_ttl = max(0.0, float(pane_cache_ttl))
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target: dict[str, int] = {}
        self._pane_cache_by_session_active: dict[str, int] = {}

    def invalidate_cache(self) -> None:
        """Drop cached tmux pane metadata after structural changes."""
        self._pane_cache_expires_at = 0.0
        self._pane_cache_by_target = {}
        self._pane_cache_by_session_active = {}

    @property
    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def require_available(self) -> None:
        if not self.available:
            raise TmuxError("tmux is not installed or not found on PATH")

    def _run(self, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        self.require_available()
        result = subprocess.run(
            [self.executable, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux command failed")
        return result

    def session_exists(self, record: SessionRecord) -> bool:
        if not self.available:
            return False
        result = self._run(["has-session", "-t", record.tmux_name], check=False)
        return result.returncode == 0

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

    def pane_target(self, record: SessionRecord) -> str:
        explicit = getattr(record, "pane_target", None)
        return explicit or f"{record.tmux_name}:0.0"

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

        result = self._run(
            ["list-panes", "-t", self.pane_target(record), "-F", "#{pane_pid}"],
            check=False,
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

    def _load_pane_maps(self) -> tuple[dict[str, int], dict[str, int]] | None:
        """Return cached tmux pane maps, refreshing with one list-panes call.

        UI surfaces can refresh together; without this cache each window/pane
        asks tmux for the same pane table and the app feels sluggish.  The cache
        is deliberately short-lived so process controls remain fresh enough for
        interactive use while avoiding repeated subprocess spawns per repaint.
        """

        now = time.monotonic()
        if self.pane_cache_ttl and now < self._pane_cache_expires_at:
            return self._pane_cache_by_target, self._pane_cache_by_session_active

        result = self._run(
            [
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}",
            ],
            check=False,
        )
        if result.returncode != 0:
            self.invalidate_cache()
            return None

        by_target: dict[str, int] = {}
        by_session_active: dict[str, int] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            session_name, window_index, pane_index, pane_id, pane_pid = parts
            try:
                pid = int(pane_pid)
            except ValueError:
                continue
            by_target[f"{session_name}:{window_index}.{pane_index}"] = pid
            if pane_id:
                by_target[pane_id if pane_id.startswith("%") else f"%{pane_id}"] = pid
            by_session_active.setdefault(session_name, pid)

        self._pane_cache_by_target = by_target
        self._pane_cache_by_session_active = by_session_active
        self._pane_cache_expires_at = now + self.pane_cache_ttl if self.pane_cache_ttl else 0.0
        return by_target, by_session_active

    def pane_pids(self, records: Sequence[SessionRecord]) -> dict[str, int | None]:
        """Resolve pane pids for many sessions using a short-lived tmux snapshot.

        Supports default ``session:win.pane`` targets and adopted ``%pane_id`` targets.
        """

        out: dict[str, int | None] = {record.id: None for record in records}
        if not records or not self.available:
            return out

        maps = self._load_pane_maps()
        if maps is None:
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
        if text:
            self._run(["send-keys", "-t", self.pane_target(record), "-l", text])
        if enter:
            self._run(["send-keys", "-t", self.pane_target(record), "C-m"])

    def capture(self, record: SessionRecord, lines: int = 200) -> str:
        if not self.session_exists(record):
            return ""
        start = f"-{max(1, lines)}"
        result = self._run(["capture-pane", "-t", self.pane_target(record), "-p", "-S", start], check=False)
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
