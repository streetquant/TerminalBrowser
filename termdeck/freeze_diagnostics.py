from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .session_store import DEFAULT_AUTH_ROOT, DEFAULT_DB_PATH
from .tmux_backend import TmuxBackend, TmuxError

_MAX_PROCESSES = 8192
_MAX_D_STATE_ROWS = 64
_MAX_AUTH_SCOPES = 512


@dataclass(frozen=True)
class PressureSample:
    some_avg10: float | None = None
    some_avg60: float | None = None
    some_avg300: float | None = None
    full_avg10: float | None = None
    full_avg60: float | None = None
    full_avg300: float | None = None


@dataclass(frozen=True)
class BlockedProcess:
    pid: int
    state: str
    comm: str
    wait_channel: str


@dataclass(frozen=True)
class AuthScopeFootprint:
    scope_id: str
    referenced: bool
    state_bytes: int
    wal_bytes: int
    shm_bytes: int


def _parse_pressure_line(line: str) -> tuple[str, dict[str, float]] | None:
    fields = line.strip().split()
    if not fields or fields[0] not in {"some", "full"}:
        return None
    values: dict[str, float] = {}
    for token in fields[1:]:
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        if key not in {"avg10", "avg60", "avg300"}:
            continue
        try:
            values[key] = float(raw)
        except ValueError:
            continue
    return fields[0], values


def read_io_pressure(path: str | Path = "/proc/pressure/io") -> PressureSample:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return PressureSample()
    parsed = dict(
        item
        for line in lines[:4]
        if (item := _parse_pressure_line(line)) is not None
    )
    some = parsed.get("some", {})
    full = parsed.get("full", {})
    return PressureSample(
        some_avg10=some.get("avg10"),
        some_avg60=some.get("avg60"),
        some_avg300=some.get("avg300"),
        full_avg10=full.get("avg10"),
        full_avg60=full.get("avg60"),
        full_avg300=full.get("avg300"),
    )


def blocked_processes(
    *,
    proc_root: str | Path = "/proc",
    max_processes: int = _MAX_PROCESSES,
    max_rows: int = _MAX_D_STATE_ROWS,
) -> tuple[BlockedProcess, ...]:
    root = Path(proc_root)
    try:
        candidates = sorted(
            (entry for entry in root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return ()
    candidates = candidates[: max(1, min(_MAX_PROCESSES, int(max_processes)))]
    rows: list[BlockedProcess] = []
    row_limit = max(1, min(_MAX_D_STATE_ROWS, int(max_rows)))
    for entry in candidates:
        if len(rows) >= row_limit:
            break
        try:
            raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        right = raw.rfind(")")
        left = raw.find("(")
        if left < 0 or right < left + 1:
            continue
        tail = raw[right + 1 :].strip().split()
        if not tail or tail[0] != "D":
            continue
        comm = raw[left + 1 : right][:128]
        try:
            wait_channel = (entry / "wchan").read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:128]
        except OSError:
            wait_channel = ""
        rows.append(
            BlockedProcess(
                pid=int(entry.name),
                state="D",
                comm=comm,
                wait_channel=wait_channel,
            )
        )
    return tuple(rows)


def _referenced_homes(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    connection = sqlite3.connect(
        f"{db_path.resolve(strict=False).as_uri()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    try:
        rows = connection.execute(
            "SELECT codex_home FROM sessions "
            "WHERE codex_home IS NOT NULL AND codex_home != ''"
        ).fetchall()
    finally:
        connection.close()
    result: set[str] = set()
    for (raw_home,) in rows:
        try:
            result.add(str(Path(str(raw_home)).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return result


def _regular_size(path: Path) -> int:
    try:
        metadata = path.lstat()
    except OSError:
        return 0
    if path.is_symlink() or not path.is_file():
        return 0
    return max(0, int(metadata.st_size))


def auth_scope_footprints(
    *,
    db_path: str | Path | None = None,
    auth_root: str | Path | None = None,
    max_scopes: int = _MAX_AUTH_SCOPES,
) -> tuple[AuthScopeFootprint, ...]:
    db = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    root = Path(auth_root).expanduser() if auth_root is not None else DEFAULT_AUTH_ROOT
    try:
        referenced = _referenced_homes(db)
        entries = sorted(
            (
                entry
                for entry in root.iterdir()
                if entry.name.startswith("auth-")
                and entry.is_dir()
                and not entry.is_symlink()
            ),
            key=lambda entry: entry.name,
        )
    except OSError:
        return ()
    limit = max(1, min(_MAX_AUTH_SCOPES, int(max_scopes)))
    rows: list[AuthScopeFootprint] = []
    for home in entries[:limit]:
        try:
            resolved = str(home.resolve(strict=False))
        except (OSError, RuntimeError):
            continue
        rows.append(
            AuthScopeFootprint(
                scope_id=home.name,
                referenced=resolved in referenced,
                state_bytes=_regular_size(home / "state_5.sqlite"),
                wal_bytes=_regular_size(home / "state_5.sqlite-wal"),
                shm_bytes=_regular_size(home / "state_5.sqlite-shm"),
            )
        )
    return tuple(rows)


def _registry_latency_ms(db_path: Path) -> float | None:
    if not db_path.is_file():
        return None
    started = time.monotonic()
    connection = sqlite3.connect(
        f"{db_path.resolve(strict=False).as_uri()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
    finally:
        connection.close()
    return round((time.monotonic() - started) * 1000.0, 3)


def _tmux_latency_ms() -> tuple[float | None, str]:
    backend = TmuxBackend()
    if not backend.available:
        return None, "tmux unavailable"
    started = time.monotonic()
    try:
        backend.list_session_names()
    except (TmuxError, OSError) as exc:
        return round((time.monotonic() - started) * 1000.0, 3), str(exc)
    return round((time.monotonic() - started) * 1000.0, 3), ""


def build_freeze_diagnostic_payload(
    *,
    db_path: str | Path | None = None,
    auth_root: str | Path | None = None,
) -> dict[str, object]:
    """Return a bounded, capture-free snapshot for Codex/terminal stalls."""

    db = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    scopes = auth_scope_footprints(db_path=db, auth_root=auth_root)
    pressure = read_io_pressure()
    blocked = blocked_processes()
    tmux_latency, tmux_error = _tmux_latency_ms()
    total_state = sum(row.state_bytes + row.wal_bytes + row.shm_bytes for row in scopes)
    largest = sorted(
        scopes,
        key=lambda row: row.state_bytes + row.wal_bytes + row.shm_bytes,
        reverse=True,
    )[:20]
    return {
        "schema": "termdeck.freeze-diagnostics.v1",
        "io_pressure": asdict(pressure),
        "blocked_processes": [asdict(row) for row in blocked],
        "registry_latency_ms": _registry_latency_ms(db),
        "tmux_control_latency_ms": tmux_latency,
        "tmux_error": tmux_error,
        "auth_scopes": {
            "sampled": len(scopes),
            "referenced": sum(1 for row in scopes if row.referenced),
            "unreferenced": sum(1 for row in scopes if not row.referenced),
            "sampled_state_bytes": total_state,
            "largest": [asdict(row) for row in largest],
        },
        "bounds": {
            "max_processes": _MAX_PROCESSES,
            "max_blocked_rows": _MAX_D_STATE_ROWS,
            "max_auth_scopes": _MAX_AUTH_SCOPES,
        },
        "privacy": {
            "environment_included": False,
            "terminal_captures_included": False,
            "process_arguments_included": False,
        },
    }
