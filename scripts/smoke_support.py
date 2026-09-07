"""Shared, read-only runtime assertions for TermDeck smoke programs.

The GUI intentionally exposes no public testing API.  Keep the small amount of
private state observation needed by end-to-end smokes in one place so transport
architecture changes cannot leave a collection of contradictory readiness
oracles behind.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


_VALIDATION_RUNTIME_ENV = "T1000_VALIDATION_RUNTIME_BASE"
_VALIDATION_RUNTIME_POLICY_ENV = "T1000_VALIDATION_RUNTIME_POLICY"
_VALIDATION_RUNTIME_MAX_PATH_BYTES = 64
_VALIDATION_RUNTIME_POLICIES = frozenset({"ci", "production"})


def require_private_validation_runtime() -> Path:
    """Return the runner-authorized private runtime required by smokes."""

    raw = str(os.environ.get(_VALIDATION_RUNTIME_ENV, "") or "").strip()
    if not raw:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_ENV} is required; refusing a /tmp fallback"
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise RuntimeError(f"{_VALIDATION_RUNTIME_ENV} must be an absolute path")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_ENV} is unavailable: {candidate}"
        ) from exc
    if (
        resolved != candidate
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_ENV} is not a private 0700 directory: {candidate}"
        )
    if len(os.fsencode(candidate)) > _VALIDATION_RUNTIME_MAX_PATH_BYTES:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_ENV} exceeds the socket path budget: {candidate}"
        )
    temporary_path = Path(tempfile.gettempdir())
    try:
        temporary_metadata = temporary_path.lstat()
        temporary_resolved = temporary_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"the effective temporary directory is unavailable: {temporary_path}"
        ) from exc
    if (
        temporary_resolved != temporary_path
        or not stat.S_ISDIR(temporary_metadata.st_mode)
        or temporary_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(temporary_metadata.st_mode) != 0o700
        or (
            temporary_resolved != candidate
            and candidate not in temporary_resolved.parents
        )
    ):
        raise RuntimeError(
            "the effective temporary directory is outside the private validation runtime: "
            f"{temporary_path}"
        )
    policy = str(os.environ.get(_VALIDATION_RUNTIME_POLICY_ENV, "") or "").strip()
    if not policy:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_POLICY_ENV} is required; refusing to infer CI portability"
        )
    if policy not in _VALIDATION_RUNTIME_POLICIES:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_POLICY_ENV} is unsupported: {policy}"
        )
    if policy == "ci":
        return candidate
    try:
        from termdeck.storage_fabric import require_hot_storage

        require_hot_storage(
            managed_root=candidate,
            hot_paths={
                "auth_root": candidate,
                "checkpoint_root": temporary_resolved,
                "database_root": candidate,
                "runtime_root": temporary_resolved,
                "tmux_runtime": candidate,
            },
            production=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"{_VALIDATION_RUNTIME_ENV} is not verified Warp Drives SSD storage: {candidate}"
        ) from exc
    return candidate


@dataclass(frozen=True)
class StreamSnapshot:
    """One lock-consistent view of an AttachTerminal stream controller."""

    controller: Any
    ready: bool
    stopped: bool
    generation: int
    size: tuple[int, int]
    state: Any


def stream_snapshot(widget: Any) -> StreamSnapshot | None:
    """Return a lock-consistent controller snapshot, or ``None`` before attach."""

    controller = getattr(widget, "controller", None)
    if controller is None:
        return None
    lock = getattr(controller, "_lock", None)

    def capture() -> StreamSnapshot:
        columns, rows = getattr(controller, "_size", (0, 0))
        return StreamSnapshot(
            controller=controller,
            ready=bool(getattr(controller, "_ready", False)),
            stopped=bool(getattr(controller, "_stopped", False)),
            generation=int(getattr(widget, "_stream_generation", 0)),
            size=(int(columns), int(rows)),
            state=getattr(controller, "state", None),
        )

    if lock is None:
        return capture()
    with lock:
        return capture()


def stream_ready(widget: Any) -> bool:
    """True only after the current controller committed setup and is live."""

    snapshot = stream_snapshot(widget)
    return snapshot is not None and snapshot.ready and not snapshot.stopped
