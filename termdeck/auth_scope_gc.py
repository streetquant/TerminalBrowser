from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .freeze_diagnostics import auth_scope_footprints
from .session_store import DEFAULT_AUTH_ROOT, DEFAULT_DB_PATH

_SCOPE_RE = re.compile(r"auth-[0-9a-f]{32}\Z")
_MAX_PROCESSES = 8192
_MAX_CMDLINE_BYTES = 64 * 1024
_MAX_TOTAL_CMDLINE_BYTES = 16 * 1024 * 1024
_DEFAULT_MIN_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class ScopeGcCandidate:
    scope_id: str
    path: str
    age_seconds: float
    referenced: bool
    managed_process_pids: tuple[int, ...]
    process_scan_complete: bool
    transaction_lock_free: bool
    legacy_lifetime_lock_free: bool
    canonical_lifetime_lock_free: bool
    state_bytes: int
    eligible: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _auth_root(path: str | Path | None) -> Path:
    root = Path(path).expanduser() if path is not None else DEFAULT_AUTH_ROOT
    root = Path(os.path.abspath(os.fspath(root)))
    metadata = root.lstat()
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise RuntimeError(f"auth root is unsafe: {root}")
    return root


def _legacy_lock(scope_id: str) -> Path:
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return Path(os.path.abspath(os.fspath(cache))) / "codex-contained-startup" / f"{scope_id}.lock"


def _canonical_lock(home: Path, scope_id: str) -> Path:
    return home / ".termdeck-codex-lifetime" / f"scope.{scope_id}.lock"


def _registry_db(path: str | Path | None) -> Path:
    candidate = Path(path).expanduser() if path is not None else DEFAULT_DB_PATH
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"TermDeck registry does not exist: {candidate}") from exc
    except OSError as exc:
        raise RuntimeError(f"TermDeck registry cannot be inspected: {candidate}: {exc}") from exc
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise RuntimeError(f"TermDeck registry is unsafe for auth-scope maintenance: {candidate}")
    return candidate


def _lock_free(path: Path, *, private: bool) -> tuple[bool, str]:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return True, "missing"
    except OSError as exc:
        return False, f"lstat failed: {exc}"
    if stat.S_ISLNK(existing.st_mode):
        return False, "symlink"
    if not stat.S_ISREG(existing.st_mode):
        return False, "not a regular file"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False, "O_NOFOLLOW unavailable"
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | nofollow)
    except OSError as exc:
        return False, f"open failed: {exc}"
    try:
        metadata = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        forbidden = 0o077 if private else 0o022
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & forbidden
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
        ):
            return False, "unsafe metadata"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, "busy"
        except OSError as exc:
            return False, f"lock failed: {exc}"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True, "free"
    finally:
        os.close(fd)


def _managed_processes_for_scopes(
    homes: tuple[Path, ...],
) -> tuple[dict[str, tuple[int, ...]], bool, str]:
    """Find TermDeck-managed Bash boundaries without reading process environments."""

    needles = {str(home): [] for home in homes}
    if not needles:
        return {}, True, ""
    try:
        processes = sorted(
            (entry for entry in Path("/proc").iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError as exc:
        return {}, False, f"cannot list /proc: {exc}"
    if len(processes) > _MAX_PROCESSES:
        return {}, False, f"process count exceeds proof limit {_MAX_PROCESSES}"
    total = 0
    for entry in processes:
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if len(raw) > _MAX_CMDLINE_BYTES:
            return {}, False, f"process {entry.name} command line exceeds proof limit"
        total += len(raw)
        if total > _MAX_TOTAL_CMDLINE_BYTES:
            return {}, False, "process command-line scan exceeded byte proof limit"
        if not raw:
            continue
        text = os.fsdecode(raw.replace(b"\0", b" "))
        for home in needles:
            if home in text:
                needles[home].append(int(entry.name))
    return {home: tuple(pids) for home, pids in needles.items()}, True, ""


def plan_auth_scope_gc(
    *,
    db_path: str | Path | None = None,
    auth_root: str | Path | None = None,
    min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS,
) -> dict[str, object]:
    """Build a bounded dry-run plan; this function never mutates auth scopes."""

    root = _auth_root(auth_root)
    db = _registry_db(db_path)
    try:
        managed_entries = sorted(
            (
                entry
                for entry in root.iterdir()
                if entry.name.startswith("auth-")
                and entry.is_dir()
                and not entry.is_symlink()
            ),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        raise RuntimeError(f"auth scope inventory cannot be listed safely: {exc}") from exc
    footprints = auth_scope_footprints(db_path=db, auth_root=root)
    truncated = len(managed_entries) > len(footprints)
    homes = tuple(root / row.scope_id for row in footprints)
    owners, complete, process_detail = _managed_processes_for_scopes(homes)
    now = time.time()
    candidates: list[ScopeGcCandidate] = []
    for row in footprints:
        home = root / row.scope_id
        reasons: list[str] = []
        try:
            metadata = home.lstat()
            safe_home = (
                not home.is_symlink()
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700
            )
            age = max(0.0, now - metadata.st_mtime)
        except OSError as exc:
            safe_home = False
            age = 0.0
            reasons.append(f"scope cannot be inspected: {exc}")
        if _SCOPE_RE.fullmatch(row.scope_id) is None:
            reasons.append("scope name is not a managed auth id")
        if not safe_home:
            reasons.append("scope directory has unsafe ownership/type/mode")
        if row.referenced:
            reasons.append("scope is referenced by the TermDeck registry")
        if age < max(0.0, float(min_age_seconds)):
            reasons.append("scope is younger than the quarantine grace period")
        pids = owners.get(str(home), ())
        if pids:
            reasons.append("a managed process command line still references this scope")
        if not complete:
            reasons.append(f"managed-process proof is incomplete: {process_detail}")
        if truncated:
            reasons.append(
                "auth scope inventory exceeds the bounded proof limit; no candidate is actionable"
            )
        tx_free, tx_detail = _lock_free(home / ".termdeck-auth-transaction.lock", private=True)
        if not tx_free:
            reasons.append(f"auth transaction lock is not free: {tx_detail}")
        legacy_free, legacy_detail = _lock_free(_legacy_lock(row.scope_id), private=False)
        if not legacy_free:
            reasons.append(f"legacy lifetime lock is not free: {legacy_detail}")
        canonical_free, canonical_detail = _lock_free(_canonical_lock(home, row.scope_id), private=False)
        if not canonical_free:
            reasons.append(f"canonical lifetime lock is not free: {canonical_detail}")
        candidates.append(
            ScopeGcCandidate(
                scope_id=row.scope_id,
                path=str(home),
                age_seconds=round(age, 3),
                referenced=row.referenced,
                managed_process_pids=pids,
                process_scan_complete=complete,
                transaction_lock_free=tx_free,
                legacy_lifetime_lock_free=legacy_free,
                canonical_lifetime_lock_free=canonical_free,
                state_bytes=row.state_bytes + row.wal_bytes + row.shm_bytes,
                eligible=not reasons,
                reasons=tuple(reasons),
            )
        )
    return {
        "ok": True,
        "action": "auth-gc-plan",
        "dry_run": True,
        "auth_root": str(root),
        "min_age_seconds": max(0.0, float(min_age_seconds)),
        "candidate_count": len(candidates),
        "scope_count_total": len(managed_entries),
        "truncated": truncated,
        "eligible_count": sum(1 for row in candidates if row.eligible),
        "process_scan_complete": complete,
        "candidates": [row.as_dict() for row in candidates],
    }


def _ensure_lock_parent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError(f"lock parent is unsafe: {path}")


@contextmanager
def _hold_lock(path: Path, *, private: bool):
    _ensure_lock_parent(path.parent)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("O_NOFOLLOW is unavailable")
    fd = os.open(
        path,
        os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | nofollow,
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        metadata = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        forbidden = 0o077 if private else 0o022
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & forbidden
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RuntimeError(f"lock metadata is unsafe: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"lock is busy: {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def quarantine_auth_scope(
    scope_id: str,
    *,
    db_path: str | Path | None = None,
    auth_root: str | Path | None = None,
    min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS,
) -> dict[str, object]:
    """Reserved quarantine primitive; intentionally disabled in this release."""

    raise RuntimeError(
        "auth-scope quarantine is disabled until its isolated mutation and "
        "rollback path can be exercised end-to-end"
    )

    if _SCOPE_RE.fullmatch(scope_id or "") is None:  # pragma: no cover
        raise RuntimeError("scope id is not a managed auth id")
    root = _auth_root(auth_root)
    home = root / scope_id
    before = home.lstat()
    if (
        home.is_symlink()
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        raise RuntimeError("scope directory is unsafe")
    if time.time() - before.st_mtime < max(0.0, float(min_age_seconds)):
        raise RuntimeError("scope is younger than the quarantine grace period")

    db = _registry_db(db_path)
    transaction_lock = home / ".termdeck-auth-transaction.lock"
    legacy_lock = _legacy_lock(scope_id)
    canonical_lock = _canonical_lock(home, scope_id)
    with ExitStack() as stack:
        stack.enter_context(_hold_lock(transaction_lock, private=True))
        stack.enter_context(_hold_lock(legacy_lock, private=False))
        stack.enter_context(_hold_lock(canonical_lock, private=False))

        owners, complete, detail = _managed_processes_for_scopes((home,))
        if not complete:
            raise RuntimeError(f"managed-process proof is incomplete: {detail}")
        pids = owners.get(str(home), ())
        if pids:
            raise RuntimeError(
                "managed process still references scope: "
                + ", ".join(str(pid) for pid in pids)
            )

        connection = sqlite3.connect(
            f"{db.as_uri()}?mode=rw",
            uri=True,
            timeout=1.0,
        )
        destination: Path | None = None
        renamed = False
        quarantine: Path | None = None
        try:
            connection.execute("PRAGMA busy_timeout = 1000")
            connection.execute("BEGIN IMMEDIATE")
            resolved_home = str(home.resolve(strict=False))
            row = connection.execute(
                "SELECT id FROM sessions WHERE codex_home = ? LIMIT 1",
                (resolved_home,),
            ).fetchone()
            if row is not None:
                raise RuntimeError("scope became referenced by the TermDeck registry")

            current = home.lstat()
            if (
                home.is_symlink()
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISDIR(current.st_mode)
                or current.st_uid != os.geteuid()
                or stat.S_IMODE(current.st_mode) != 0o700
            ):
                raise RuntimeError("scope identity changed while proving quarantine safety")

            quarantine = root / ".quarantine"
            quarantine.mkdir(mode=0o700, exist_ok=True)
            qmeta = quarantine.lstat()
            if (
                quarantine.is_symlink()
                or not stat.S_ISDIR(qmeta.st_mode)
                or qmeta.st_uid != os.geteuid()
                or stat.S_IMODE(qmeta.st_mode) != 0o700
            ):
                raise RuntimeError("quarantine directory is unsafe")
            destination = quarantine / (
                f"{scope_id}.{int(time.time())}.{uuid.uuid4().hex[:8]}"
            )
            home.rename(destination)
            renamed = True
            for directory in (root, quarantine):
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if renamed and destination is not None and destination.exists() and not home.exists():
                try:
                    destination.rename(home)
                    for directory in (root, quarantine):
                        if directory is None:
                            continue
                        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        "auth scope quarantine failed and rename rollback was incomplete: "
                        f"{rollback_exc}"
                    ) from exc
            raise
        finally:
            connection.close()


    return {
        "ok": True,
        "action": "auth-gc-quarantine",
        "scope_id": scope_id,
        "source": str(home),
        "quarantine": str(destination) if destination is not None else "",
        "deleted": False,
    }
