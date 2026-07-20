from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import SessionRecord
from .session_store import APP_DIR

CHECKPOINT_SCHEMA = "termdeck.process-checkpoint.v1"
DEFAULT_CHECKPOINT_ROOT = APP_DIR / "checkpoints"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointResult:
    session_id: str
    action: str
    ok: bool
    exact: bool
    detail: str
    image_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.session_id,
            "action": self.action,
            "ok": self.ok,
            "exact": self.exact,
            "detail": self.detail,
            "image_count": self.image_count,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DmtcpCheckpointManager:
    """Checkpoint TermDeck-managed process trees with DMTCP.

    Each tab owns one DMTCP coordinator and checkpoint directory. A completed
    checkpoint is exposed only after all image files exist and an atomic local
    manifest has been written. The previous completed generation is retained
    until the next generation commits, so an interrupted checkpoint cannot
    destroy the last restorable state.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        launch_executable: str = "dmtcp_launch",
        command_executable: str = "dmtcp_command",
        restart_executable: str = "dmtcp_restart",
        timeout_seconds: float = 180.0,
    ) -> None:
        self.root = Path(root).expanduser() if root else DEFAULT_CHECKPOINT_ROOT
        self.launch_executable = launch_executable
        self.command_executable = command_executable
        self.restart_executable = restart_executable
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    @staticmethod
    def _resolved_executable(name: str) -> str | None:
        return shutil.which(name)

    @property
    def available(self) -> bool:
        return all(
            self._resolved_executable(name)
            for name in (
                self.launch_executable,
                self.command_executable,
                self.restart_executable,
            )
        )

    def unavailable_detail(self) -> str:
        missing = [
            name
            for name in (
                self.launch_executable,
                self.command_executable,
                self.restart_executable,
            )
            if self._resolved_executable(name) is None
        ]
        return "missing DMTCP tools: " + ", ".join(missing) if missing else "DMTCP is available"

    def session_dir(self, record: SessionRecord) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(record.id):
            raise CheckpointError(f"unsafe session id: {record.id!r}")
        return self.root / record.id

    def port_file(self, record: SessionRecord) -> Path:
        return self.session_dir(record) / "coordinator.port"

    def manifest_file(self, record: SessionRecord) -> Path:
        return self.session_dir(record) / "manifest.json"

    def _image_files(self, record: SessionRecord) -> list[Path]:
        directory = self.session_dir(record)
        return sorted(
            path
            for path in directory.glob("ckpt_*.dmtcp")
            if path.is_file() and path.stat().st_size > 0
        )

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _load_manifest(self, record: SessionRecord) -> dict[str, object] | None:
        path = self.manifest_file(record)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != CHECKPOINT_SCHEMA or payload.get("session_id") != record.id:
            return None
        names = payload.get("files")
        if not isinstance(names, list) or not names:
            return None
        directory = self.session_dir(record)
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or not name.startswith("ckpt_") or not name.endswith(".dmtcp"):
                return None
            image = directory / name
            try:
                if not image.is_file() or image.stat().st_size <= 0:
                    return None
            except OSError:
                return None
        return payload

    def has_checkpoint(self, record: SessionRecord) -> bool:
        return self._load_manifest(record) is not None

    def wrap_launch_command(self, record: SessionRecord, command: str) -> str:
        """Return a shell-safe command that starts one tab under DMTCP control."""

        if not self.available or getattr(record, "external", False):
            return command
        directory = self.session_dir(record)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            self.port_file(record).unlink()
        except FileNotFoundError:
            pass
        launcher = self._resolved_executable(self.launch_executable) or self.launch_executable
        return shlex.join(
            [
                launcher,
                "--new-coordinator",
                "--coord-port",
                "0",
                "--port-file",
                str(self.port_file(record)),
                "--ckptdir",
                str(directory),
                "--ckpt-open-files",
                "/bin/sh",
                "-lc",
                command,
            ]
        )

    def restart_command(self, record: SessionRecord) -> str | None:
        """Build a trusted restart command from a validated local manifest."""

        if not self.available or getattr(record, "external", False):
            return None
        manifest = self._load_manifest(record)
        if manifest is None:
            return None
        directory = self.session_dir(record)
        try:
            self.port_file(record).unlink()
        except FileNotFoundError:
            pass
        restart = self._resolved_executable(self.restart_executable) or self.restart_executable
        files = [str(directory / str(name)) for name in manifest["files"]]  # type: ignore[index]
        return shlex.join(
            [
                restart,
                "--new-coordinator",
                "--coord-port",
                "0",
                "--port-file",
                str(self.port_file(record)),
                "--ckptdir",
                str(directory),
                *files,
            ]
        )

    def _read_port(self, record: SessionRecord) -> int:
        try:
            value = int(self.port_file(record).read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise CheckpointError("DMTCP coordinator port is unavailable") from exc
        if not 1 <= value <= 65535:
            raise CheckpointError(f"invalid DMTCP coordinator port: {value}")
        return value

    def _move_previous_generation(self, record: SessionRecord) -> Path:
        directory = self.session_dir(record)
        previous = directory / ".previous"
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        previous.mkdir(parents=True, exist_ok=True)
        for path in [*self._image_files(record), self.manifest_file(record)]:
            if path.exists():
                os.replace(path, previous / path.name)
        return previous

    def _restore_previous_generation(self, record: SessionRecord, previous: Path) -> None:
        for path in self._image_files(record):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self.manifest_file(record).unlink()
        except OSError:
            pass
        if previous.exists():
            for path in previous.iterdir():
                if path.is_file():
                    os.replace(path, self.session_dir(record) / path.name)
            shutil.rmtree(previous, ignore_errors=True)

    def checkpoint(self, record: SessionRecord) -> CheckpointResult:
        if getattr(record, "external", False):
            return CheckpointResult(record.id, "checkpoint", False, False, "external tmux sessions cannot be checkpointed")
        if not self.available:
            return CheckpointResult(record.id, "checkpoint", False, False, self.unavailable_detail())
        try:
            coordinator_port = self._read_port(record)
        except CheckpointError as exc:
            return CheckpointResult(record.id, "checkpoint", False, False, str(exc))

        directory = self.session_dir(record)
        directory.mkdir(parents=True, exist_ok=True)
        previous = self._move_previous_generation(record)
        command = self._resolved_executable(self.command_executable) or self.command_executable
        try:
            completed = subprocess.run(
                [
                    command,
                    "--coord-port",
                    str(coordinator_port),
                    "--bcheckpoint",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._restore_previous_generation(record, previous)
            return CheckpointResult(record.id, "checkpoint", False, False, f"checkpoint command failed: {exc}")

        if completed.returncode != 0:
            self._restore_previous_generation(record, previous)
            detail = (completed.stderr or completed.stdout or "DMTCP checkpoint failed").strip()
            return CheckpointResult(record.id, "checkpoint", False, False, detail)

        deadline = time.monotonic() + min(10.0, self.timeout_seconds)
        images = self._image_files(record)
        while not images and time.monotonic() < deadline:
            time.sleep(0.05)
            images = self._image_files(record)
        if not images:
            self._restore_previous_generation(record, previous)
            return CheckpointResult(record.id, "checkpoint", False, False, "DMTCP produced no checkpoint images")

        manifest = {
            "schema": CHECKPOINT_SCHEMA,
            "session_id": record.id,
            "created_at": _utc_now(),
            "command": record.command,
            "cwd": record.cwd,
            "files": [path.name for path in images],
            "sizes": {path.name: path.stat().st_size for path in images},
            "exact": True,
        }
        try:
            self._atomic_write_json(self.manifest_file(record), manifest)
        except OSError as exc:
            self._restore_previous_generation(record, previous)
            return CheckpointResult(record.id, "checkpoint", False, False, f"could not commit checkpoint manifest: {exc}")
        shutil.rmtree(previous, ignore_errors=True)
        return CheckpointResult(record.id, "checkpoint", True, True, "process state checkpointed", len(images))

    def checkpoint_many(self, records: Iterable[SessionRecord]) -> list[CheckpointResult]:
        return [self.checkpoint(record) for record in records]

    def clear(self, record: SessionRecord) -> None:
        try:
            shutil.rmtree(self.session_dir(record))
        except OSError:
            return
