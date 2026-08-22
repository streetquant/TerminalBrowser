from __future__ import annotations

"""Validated gateway-model inventory and stale-process detection.

Claude Code currently memoizes ``gateway-models.json`` inside each process.
Updating the shared cache therefore makes a process started before that cache
write stale, while a process started afterwards sees the current inventory.
This module only establishes that evidence; process replacement remains an
explicit higher-level action because an automatic restart could discard an
unsent composer draft.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import stat
from typing import Iterable

DEFAULT_GATEWAY_MODEL_CACHE = Path.home() / ".claude" / "cache" / "gateway-models.json"
_MAX_CACHE_BYTES = 4 * 1024 * 1024
_MAX_MODELS = 10_000


class GatewayModelError(ValueError):
    """Raised when the local gateway-model cache is unsafe or malformed."""


@dataclass(frozen=True)
class GatewayModelInventory:
    path: Path
    base_url: str
    fetched_at: str | int | float | None
    model_ids: tuple[str, ...]
    fingerprint: str
    mtime: float
    mtime_ns: int

    @property
    def count(self) -> int:
        return len(self.model_ids)


@dataclass(frozen=True)
class GatewayModelStatus:
    applicable: bool
    available: bool
    stale: bool
    model_count: int = 0
    fingerprint: str | None = None
    cache_mtime: float | None = None
    process_started_at: float | None = None
    detail: str = ""


def _canonical_fingerprint(base_url: str, model_ids: Iterable[str]) -> str:
    payload = json.dumps(
        {"base_url": base_url, "models": sorted(set(model_ids))},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_gateway_model_inventory(
    path: str | Path | None = None,
) -> GatewayModelInventory:
    candidate = Path(path or DEFAULT_GATEWAY_MODEL_CACHE).expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise GatewayModelError(f"gateway model cache is unavailable: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
        raise GatewayModelError("gateway model cache must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_CACHE_BYTES:
        raise GatewayModelError("gateway model cache has an invalid size")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags | nofollow)
    except OSError as exc:
        raise GatewayModelError(f"gateway model cache could not be opened safely: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise GatewayModelError("gateway model cache changed while being opened")
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        overflow = os.read(fd, 1)
        closed_snapshot = os.fstat(fd)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        final_metadata = candidate.lstat()
    except OSError as exc:
        raise GatewayModelError(
            f"gateway model cache disappeared while being read: {exc}"
        ) from exc
    if (
        remaining
        or overflow
        or len(raw) != metadata.st_size
        or closed_snapshot.st_dev != metadata.st_dev
        or closed_snapshot.st_ino != metadata.st_ino
        or closed_snapshot.st_size != metadata.st_size
        or closed_snapshot.st_mtime_ns != metadata.st_mtime_ns
        or final_metadata.st_dev != metadata.st_dev
        or final_metadata.st_ino != metadata.st_ino
        or final_metadata.st_size != metadata.st_size
        or final_metadata.st_mtime_ns != metadata.st_mtime_ns
    ):
        raise GatewayModelError("gateway model cache changed while being read")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise GatewayModelError("gateway model cache is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise GatewayModelError("gateway model cache root must be an object")
    base_url = payload.get("baseUrl")
    rows = payload.get("models")
    fetched_at = payload.get("fetchedAt")
    if (
        not isinstance(base_url, str)
        or not base_url.strip()
        or "\x00" in base_url
        or len(base_url.encode("utf-8")) > 4096
    ):
        raise GatewayModelError("gateway model cache baseUrl is missing or invalid")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_MODELS:
        raise GatewayModelError("gateway model cache models list is invalid")
    if fetched_at is not None:
        if isinstance(fetched_at, bool) or not isinstance(
            fetched_at, (str, int, float)
        ):
            raise GatewayModelError("gateway model cache fetchedAt is invalid")
        if isinstance(fetched_at, str) and (
            "\x00" in fetched_at or len(fetched_at.encode("utf-8")) > 256
        ):
            raise GatewayModelError("gateway model cache fetchedAt is invalid")
        if isinstance(fetched_at, (int, float)) and (
            not math.isfinite(float(fetched_at)) or float(fetched_at) < 0
        ):
            raise GatewayModelError("gateway model cache fetchedAt is invalid")

    model_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise GatewayModelError("gateway model cache contains a non-object model")
        model_id = row.get("id")
        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or "\x00" in model_id
            or len(model_id.encode("utf-8")) > 1024
        ):
            raise GatewayModelError("gateway model cache contains an invalid model id")
        model_ids.append(model_id.strip())
    canonical_ids = tuple(sorted(set(model_ids)))
    if not canonical_ids:
        raise GatewayModelError("gateway model cache contains no model ids")
    return GatewayModelInventory(
        path=candidate,
        base_url=base_url.strip(),
        fetched_at=fetched_at,
        model_ids=canonical_ids,
        fingerprint=_canonical_fingerprint(base_url.strip(), canonical_ids),
        mtime=metadata.st_mtime,
        mtime_ns=metadata.st_mtime_ns,
    )


def _launcher_name(command: object) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    return Path(argv[0]).name.casefold()


def record_uses_gateway_models(record: object) -> bool:
    if bool(getattr(record, "external", False)):
        return False
    for command in (
        getattr(record, "recovery_command", None),
        getattr(record, "command", None),
    ):
        if _launcher_name(command) == "cc-go-ulw":
            return True
    return False


def process_start_epoch(pid: int | None) -> float | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        rest = raw.rsplit(") ", 1)[1].split()
        start_ticks = int(rest[19])
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        boot_time = None
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                boot_time = int(line.split()[1])
                break
        if boot_time is None or clock_ticks <= 0:
            return None
        return boot_time + (start_ticks / clock_ticks)
    except (IndexError, OSError, RuntimeError, ValueError):
        return None


def gateway_model_status(
    record: object,
    *,
    process_started_at: float | None,
    cache_path: str | Path | None = None,
) -> GatewayModelStatus:
    if not record_uses_gateway_models(record):
        return GatewayModelStatus(False, False, False, detail="not a gateway-model session")
    try:
        inventory = load_gateway_model_inventory(cache_path)
    except GatewayModelError as exc:
        return GatewayModelStatus(True, False, False, detail=str(exc))
    stale = bool(
        process_started_at is not None
        and process_started_at + 0.001 < inventory.mtime
    )
    detail = (
        "gateway model cache is newer than the running process"
        if stale
        else (
            "running process started after the gateway model cache"
            if process_started_at is not None
            else "running process start time is unavailable"
        )
    )
    return GatewayModelStatus(
        applicable=True,
        available=True,
        stale=stale,
        model_count=inventory.count,
        fingerprint=inventory.fingerprint,
        cache_mtime=inventory.mtime,
        process_started_at=process_started_at,
        detail=detail,
    )
