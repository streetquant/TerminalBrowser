"""Read-only inventory for a TermDeck state migration boundary.

The migration itself lives in the deployment transaction.  This module only
observes a source tree: it records the ``sessions.sqlite3`` cohort, counts the
recursive ``terminal-auth`` SQLite cohorts, and checks for processes holding
those files.  It never opens SQLite and never writes to the inspected tree.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import os
from pathlib import Path
import stat
import time
from typing import cast


SCHEMA = "termdeck.state-inventory.v1"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_PRIMARY_SUFFIXES = (".sqlite3", ".sqlite", ".db")
_DEFAULT_HOLDER_TIMEOUT_SECONDS = 2.0
_DEFAULT_HOLDER_MAX_PROCESSES = 1024
_DEFAULT_HOLDER_MAX_DESCRIPTORS = 32768


class StateInventoryError(RuntimeError):
    """Raised when a state inventory cannot be completed safely."""


def _canonical_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise StateInventoryError(f"{label} must be an absolute path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StateInventoryError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise StateInventoryError(f"{label} must be a real directory: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StateInventoryError(f"{label} cannot be resolved: {path}") from exc
    if resolved != path:
        raise StateInventoryError(f"{label} is aliased by a symlink: {path}")
    return path


def _identity(path: Path, *, digest: bool = False, label: str = "file") -> dict[str, object]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise StateInventoryError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StateInventoryError(f"{label} must be a regular file: {path}")
    value: dict[str, object] = {
        "path": str(path),
        "dev": int(before.st_dev),
        "ino": int(before.st_ino),
        "mode": stat.S_IMODE(before.st_mode),
        "size": int(before.st_size),
        "mtime_ns": int(before.st_mtime_ns),
        "ctime_ns": int(before.st_ctime_ns),
        "nlink": int(before.st_nlink),
    }
    if digest:
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            after = path.lstat()
        except OSError as exc:
            raise StateInventoryError(f"{label} changed while being read: {path}") from exc
        if (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        ) != (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        ):
            raise StateInventoryError(f"{label} changed while being read: {path}")
        value["sha256"] = hasher.hexdigest()
    return value


def _classify(name: str) -> tuple[str, str] | None:
    """Return ``(base-name, role)`` for a SQLite-like filename."""

    for suffix, role in (
        ("-wal", "wal"),
        ("-shm", "shm"),
        ("-journal", "journal"),
        (".wal", "wal"),
        (".shm", "shm"),
        (".journal", "journal"),
    ):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], role
    for suffix in _PRIMARY_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name, "primary"
    # Keep unusual SQLite-adjacent names visible without treating them as a
    # complete cohort.  This catches e.g. ``foo.sqlite3.bak`` in readback.
    if ".sqlite" in name or name.endswith(".db"):
        return name, "other"
    return None


def _sqlite_cohort(primary: Path) -> dict[str, object]:
    primary_identity = _identity(primary, digest=True, label="sessions.sqlite3")
    components: dict[str, dict[str, object]] = {"primary": primary_identity}
    paths = [primary]
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = primary.with_name(primary.name + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StateInventoryError(f"could not inspect SQLite sidecar: {sidecar}") from exc
        item = _identity(sidecar, digest=True, label=f"sessions.sqlite3{suffix}")
        components[suffix[1:]] = item
        paths.append(sidecar)
    return {
        "source_path": str(primary),
        "source_paths": [str(path) for path in paths],
        "components": components,
        "component_count": len(components),
    }


def _walk_terminal_auth(root: Path) -> tuple[dict[str, int], dict[tuple[int, int], str]]:
    counts: dict[str, int] = {
        "directories": 0,
        "top_level_directories": 0,
        "files": 0,
        "bytes": 0,
        "symlinks": 0,
        "special_files": 0,
        "primary_count": 0,
        "wal_count": 0,
        "wal_nonzero_count": 0,
        "shm_count": 0,
        "journal_count": 0,
        "other_sqlite_related_count": 0,
        "sqlite_related_bytes": 0,
        "wal_bytes": 0,
        "shm_bytes": 0,
        "journal_bytes": 0,
    }
    primaries: set[str] = set()
    sidecars: dict[tuple[str, str], int] = defaultdict(int)
    identities: dict[tuple[int, int], str] = {}
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for directory, dirnames, filenames in walker:
            current = Path(directory)
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                path = current / name
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise StateInventoryError(f"could not inspect terminal-auth entry: {path}") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    counts["symlinks"] += 1
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    counts["special_files"] += 1
                    continue
                kept_dirs.append(name)
                counts["directories"] += 1
                if current == root:
                    counts["top_level_directories"] += 1
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                path = current / name
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise StateInventoryError(f"could not inspect terminal-auth entry: {path}") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    counts["symlinks"] += 1
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    counts["special_files"] += 1
                    continue
                relative = path.relative_to(root).as_posix()
                kind = _classify(name)
                counts["files"] += 1
                counts["bytes"] += int(metadata.st_size)
                identities[(int(metadata.st_dev), int(metadata.st_ino))] = relative
                if kind is None:
                    continue
                base, role = kind
                if role == "primary":
                    primaries.add(relative)
                    counts["primary_count"] += 1
                    counts["sqlite_related_bytes"] += int(metadata.st_size)
                elif role in {"wal", "shm", "journal"}:
                    base_relative = str(Path(relative).with_name(base))
                    sidecars[(base_relative, role)] += 1
                    size = int(metadata.st_size)
                    counts[f"{role}_count"] += 1
                    counts[f"{role}_bytes"] += size
                    counts["sqlite_related_bytes"] += size
                    if role == "wal" and size:
                        counts["wal_nonzero_count"] += 1
                else:
                    counts["other_sqlite_related_count"] += 1
                    counts["sqlite_related_bytes"] += int(metadata.st_size)
    except OSError as exc:
        raise StateInventoryError(f"could not enumerate terminal-auth tree: {root}") from exc

    cohort_bases = {base for base, _role in sidecars}
    complete = sum(
        base in primaries
        and sidecars.get((base, "wal"), 0) > 0
        and sidecars.get((base, "shm"), 0) > 0
        for base in cohort_bases
    )
    orphan = sum(base not in primaries for base, _role in sidecars)
    counts["cohort_count"] = len(cohort_bases)
    counts["complete_cohort_count"] = int(complete)
    counts["orphan_sidecar_count"] = int(orphan)
    counts["sqlite_related_files"] = sum(
        counts[key]
        for key in (
            "primary_count",
            "wal_count",
            "shm_count",
            "journal_count",
            "other_sqlite_related_count",
        )
    )
    return counts, identities


def _process_name(process: Path) -> str:
    try:
        for line in (process / "status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:\t"):
                return line.partition("\t")[2]
    except OSError:
        pass
    return "?"


def _process_uid(process: Path) -> int | None:
    try:
        for line in (process / "status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Uid:\t"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        return -1
    except (OSError, ValueError, IndexError):
        return None
    return None


def _holders(
    identities: dict[tuple[int, int], str],
    *,
    direct_identities: dict[str, tuple[int, int]],
    known_roots: tuple[Path, ...],
    proc_root: Path = Path("/proc"),
    timeout_seconds: float = _DEFAULT_HOLDER_TIMEOUT_SECONDS,
    max_processes: int = _DEFAULT_HOLDER_MAX_PROCESSES,
    max_descriptors: int = _DEFAULT_HOLDER_MAX_DESCRIPTORS,
) -> tuple[list[dict[str, object]], bool]:
    if timeout_seconds <= 0:
        raise ValueError("holder scan timeout must be positive")
    if max_processes < 1 or max_descriptors < 1:
        raise ValueError("holder scan limits must be positive")
    holders: dict[int, dict[str, object]] = {}
    complete = True
    deadline = time.monotonic() + timeout_seconds
    process_count = 0
    descriptor_count = 0
    budget_exhausted = False
    root_strings = tuple(str(root) for root in known_roots)
    try:
        for process in proc_root.iterdir():
            if not process.name.isdecimal():
                continue
            if time.monotonic() >= deadline or process_count >= max_processes:
                complete = False
                budget_exhausted = True
                break
            process_count += 1
            process_uid = _process_uid(process)
            if process_uid == -1:
                continue
            if process_uid is None:
                complete = False
                continue
            if process_uid != os.geteuid():
                # An unprivileged caller cannot inspect another UID's
                # descriptors. That process may still be privileged, so keep
                # the inventory fail-closed instead of claiming quiescence.
                complete = False
                continue
            try:
                pid = int(process.name)
                descriptors = (process / "fd").iterdir()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                complete = False
                continue
            except OSError as exc:
                raise StateInventoryError("cannot inspect procfs state holders") from exc
            for descriptor in descriptors:
                if time.monotonic() >= deadline or descriptor_count >= max_descriptors:
                    complete = False
                    budget_exhausted = True
                    break
                descriptor_count += 1
                try:
                    link = os.readlink(descriptor)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    complete = False
                    continue
                except OSError as exc:
                    raise StateInventoryError("cannot inspect procfs state holders") from exc
                link_path = link.removesuffix(" (deleted)")
                relative: str | None = None
                direct_identity = direct_identities.get(link_path)
                if direct_identity is not None:
                    relative = identities.get(direct_identity)
                elif any(
                    link_path == root or link_path.startswith(root + os.sep)
                    for root in root_strings
                ):
                    # Hard-link aliases still need an identity check, but
                    # unrelated descriptors are rejected before stat'ing them.
                    try:
                        metadata = descriptor.stat()
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    except PermissionError:
                        complete = False
                        continue
                    except OSError as exc:
                        raise StateInventoryError("cannot inspect procfs state holders") from exc
                    relative = identities.get((int(metadata.st_dev), int(metadata.st_ino)))
                if relative is None:
                    continue
                item = holders.setdefault(
                    pid,
                    {"pid": pid, "name": _process_name(process), "file_count": 0, "paths": []},
                )
                item["file_count"] = int(cast(int, item["file_count"])) + 1
                paths = cast(list[str], item["paths"])
                if relative not in paths:
                    paths.append(relative)
            if budget_exhausted:
                break
    except OSError as exc:
        raise StateInventoryError("cannot inspect procfs for state holders") from exc
    result = sorted(holders.values(), key=lambda item: int(cast(int, item["pid"])))
    for item in result:
        item["paths"] = sorted(str(path) for path in cast(list[str], item["paths"]))
    return result, complete


def inventory_state(
    source_root: str | Path,
    *,
    terminal_auth_root: str | Path | None = None,
    check_holders: bool = True,
    require_quiescent: bool = False,
    require_complete_cohorts: bool = False,
    proc_root: str | Path = "/proc",
    holder_timeout_seconds: float = _DEFAULT_HOLDER_TIMEOUT_SECONDS,
    holder_max_processes: int = _DEFAULT_HOLDER_MAX_PROCESSES,
    holder_max_descriptors: int = _DEFAULT_HOLDER_MAX_DESCRIPTORS,
) -> dict[str, object]:
    """Return a read-only, redaction-safe state inventory.

    ``source_root`` is the singular source path accepted by older callers;
    the returned manifest also emits ``source_paths`` so callers that model a
    migration as several roots can consume the same receipt without guessing
    which spelling was intended.  Only the session DB is content-hashed.  The
    recursive auth tree is counted and identity-scanned without reading auth
    values. Holder inspection is bounded; exhausting any holder-scan budget
    marks the result incomplete and therefore cannot satisfy quiescence.
    """

    state = _canonical_directory(source_root, "TermDeck state source root")
    auth = _canonical_directory(
        terminal_auth_root or state / "terminal-auth",
        "TermDeck terminal-auth source root",
    )
    try:
        auth.relative_to(state)
    except ValueError as exc:
        raise StateInventoryError("terminal-auth source root must be inside state source root") from exc
    cohort = _sqlite_cohort(state / "sessions.sqlite3")
    auth_counts, auth_identities = _walk_terminal_auth(auth)
    holder_identities = dict(auth_identities)
    components = cast(dict[str, dict[str, object]], cohort["components"])
    for item in components.values():
        holder_identities[
            (int(cast(int, item["dev"])), int(cast(int, item["ino"])))
        ] = "../" + Path(str(item["path"])).name
    holder_rows: list[dict[str, object]] = []
    holder_scan_complete = True
    if check_holders:
        direct_identities = {
            str(item["path"]): (
                int(cast(int, item["dev"])),
                int(cast(int, item["ino"])),
            )
            for item in components.values()
        }
        direct_identities.update(
            {
                str(auth / relative): identity
                for identity, relative in auth_identities.items()
            }
        )
        holder_rows, holder_scan_complete = _holders(
            holder_identities,
            direct_identities=direct_identities,
            known_roots=(state, auth),
            proc_root=Path(proc_root),
            timeout_seconds=holder_timeout_seconds,
            max_processes=holder_max_processes,
            max_descriptors=holder_max_descriptors,
        )
    quiescent = holder_scan_complete and not holder_rows
    if require_quiescent and not quiescent:
        if not holder_scan_complete:
            raise StateInventoryError("cannot prove TermDeck state quiescence")
        raise StateInventoryError("TermDeck state is still held open")
    if require_complete_cohorts and auth_counts["orphan_sidecar_count"] != 0:
        raise StateInventoryError("terminal-auth SQLite sidecar cohorts are incomplete")
    return {
        "schema": SCHEMA,
        "source_root": str(state),
        "source_path": str(state),
        "source_paths": [str(state), str(auth)],
        "sessions_database": cohort,
        "terminal_auth": {
            "source_root": str(auth),
            "source_path": str(auth),
            "source_paths": [str(auth)],
            "counts": auth_counts,
        },
        "quiescence": {
            "checked": bool(check_holders),
            "proc_scan_complete": holder_scan_complete,
            "quiescent": quiescent,
            "holders": holder_rows,
        },
    }


__all__ = ["SCHEMA", "StateInventoryError", "inventory_state"]
