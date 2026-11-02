from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    RECOVERY_INTENT_ACTIVE,
    RECOVERY_INTENT_INACTIVE,
    RECOVERY_INTENT_LEGACY,
    RECOVERY_INTENTS,
    SessionRecord,
)
from .session_recipe import (
    DEFAULT_RESTART_POLICY,
    SessionRecipeError,
    parse_session_recipe,
    receipt_reenrollment,
    record_recipe_fields,
    recovery_command_specificity,
)
from .recovery_authority_context import (
    AuthorityObservation,
    RecoveryAuthorityError,
    bind_context,
    context_matches_record,
    record_cas,
    intent_digest,
    retain_stronger_context,
    parse_context,
)
from .storage_fabric import (
    DatabaseFileBinding,
    StorageFabricError,
    _PinnedDirectory,
    _chmod_sqlite_inode,
    _directory_identity,
    _open_pinned_directory,
    _open_sqlite_inode,
    pin_production_database,
    pin_production_database_parent,
)

def legacy_state_dir() -> Path:
    return Path.home() / ".local" / "share" / "termdeck"


def managed_state_dir(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the mutable TermDeck state directory for the active launcher.

    Managed T1000 launches place their root on the SSD fabric.  Keeping the
    TermDeck directory beside that root makes the session registry and its
    private auth scopes follow the managed state volume without moving the
    user's legacy auth tree.
    """

    values = os.environ if environ is None else environ
    raw_state_root = str(values.get("TERMDECK_STATE_ROOT", "") or "").strip()
    if raw_state_root:
        state_root = Path(raw_state_root).expanduser()
        if not state_root.is_absolute():
            raise ValueError("TERMDECK_STATE_ROOT must be an absolute path")
        return Path(os.path.abspath(os.fspath(state_root)))
    raw_root = str(values.get("T1000_MANAGED_ROOT", "") or "").strip()
    if not raw_root:
        return legacy_state_dir()
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise ValueError("T1000_MANAGED_ROOT must be an absolute path")
    return root.parent / "termdeck"


def default_db_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return managed_state_dir(environ=environ) / "sessions.sqlite3"


def default_auth_root(*, environ: Mapping[str, str] | None = None) -> Path:
    return managed_state_dir(environ=environ) / "terminal-auth"


APP_DIR = managed_state_dir()
DEFAULT_DB_PATH = default_db_path()
DEFAULT_AUTH_ROOT = default_auth_root()


class SessionDatabaseMigrationError(RuntimeError):
    """Raised when a legacy session database cannot be migrated safely."""


class SessionDatabaseMigrationInProgress(SessionDatabaseMigrationError):
    """Raised when another primary process owns the migration transaction."""


@dataclass(frozen=True)
class SessionDatabaseMigration:
    status: str
    source: Path
    destination: Path
    source_sha256: str | None = None
    destination_sha256: str | None = None
    receipt_path: Path | None = None
    backup_path: Path | None = None
    database_id: str | None = None


_MIGRATION_RECEIPT_SCHEMA = "termdeck.session-database-migration.v2"
_MIGRATION_STATE_TABLE = "termdeck_migration_state"
_MIGRATION_COMPARE_ROW_LIMIT = 100_000


@dataclass(frozen=True)
class _DatabaseSummary:
    schema_sha256: str
    content_sha256: str
    user_version: int
    session_count: int
    remembered_window_count: int
    distinct_window_count: int
    captain_count: int
    total_row_count: int
    table_counts: tuple[tuple[str, int], ...]

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")

_ACTIVE_RECOVERY_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending"}
)
_INACTIVE_RECOVERY_STATUSES = frozenset(
    {"new", "remembered", "stopped", "archived"}
)


def normalise_recovery_intent(value: object) -> str:
    """Return the canonical persisted recovery-intent value.

    Unknown values become ``legacy`` so malformed historical state cannot
    authorize a startup relaunch.  The spelling remains British to match the
    existing TermDeck normalisation helpers.
    """

    if isinstance(value, bool):
        return RECOVERY_INTENT_ACTIVE if value else RECOVERY_INTENT_INACTIVE
    normalized = str(value or "").strip().casefold()
    aliases = {
        "active": RECOVERY_INTENT_ACTIVE,
        "eligible": RECOVERY_INTENT_ACTIVE,
        "recover": RECOVERY_INTENT_ACTIVE,
        "recovery": RECOVERY_INTENT_ACTIVE,
        "resume": RECOVERY_INTENT_ACTIVE,
        "running": RECOVERY_INTENT_ACTIVE,
        "true": RECOVERY_INTENT_ACTIVE,
        "yes": RECOVERY_INTENT_ACTIVE,
        "1": RECOVERY_INTENT_ACTIVE,
        "inactive": RECOVERY_INTENT_INACTIVE,
        "archived": RECOVERY_INTENT_INACTIVE,
        "manual": RECOVERY_INTENT_INACTIVE,
        "never": RECOVERY_INTENT_INACTIVE,
        "stopped": RECOVERY_INTENT_INACTIVE,
        "terminated": RECOVERY_INTENT_INACTIVE,
        "false": RECOVERY_INTENT_INACTIVE,
        "no": RECOVERY_INTENT_INACTIVE,
        "0": RECOVERY_INTENT_INACTIVE,
        "legacy": RECOVERY_INTENT_LEGACY,
    }
    return aliases.get(normalized, RECOVERY_INTENT_LEGACY)


def recovery_intent_for_status(status: object) -> str:
    """Infer safe compatibility intent for a status-only legacy row."""

    normalized = str(status or "").strip().casefold()
    if normalized in _ACTIVE_RECOVERY_STATUSES:
        return RECOVERY_INTENT_ACTIVE
    if normalized in _INACTIVE_RECOVERY_STATUSES:
        return RECOVERY_INTENT_INACTIVE
    # A start error does not prove that a task ever became active.  Keep it
    # ambiguous until an explicit successful launch or recovery intent exists.
    return RECOVERY_INTENT_LEGACY



def utc_now() -> str:
    # Browser-style close/reopen ordering needs more than second precision:
    # several tabs can be closed in one keyboard chord burst.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def short_id(prefix: str = "td") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def tmux_safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "session"
    if cleaned[0].isdigit():
        cleaned = f"s_{cleaned}"
    return cleaned[:96]


def _absolute_migration_path(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate == Path(":memory:"):
        raise SessionDatabaseMigrationError(f"{label} cannot be an in-memory database")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _migration_file(path: str | Path, *, label: str) -> Path:
    absolute = _absolute_migration_path(path, label=label)
    current = absolute
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SessionDatabaseMigrationError(f"cannot inspect {label}") from exc
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise SessionDatabaseMigrationError(
                    f"{label} contains a symlink: {current}"
                )
            if current == absolute and not stat.S_ISREG(metadata.st_mode):
                raise SessionDatabaseMigrationError(f"{label} must be a regular file")
            if current != absolute and not stat.S_ISDIR(metadata.st_mode):
                raise SessionDatabaseMigrationError(
                    f"{label} ancestor must be a directory: {current}"
                )
        if current.parent == current:
            break
        current = current.parent
    return absolute


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _LegacySnapshotFile:
    name: str
    descriptor: int
    signature: tuple[int, int, int, int, int]
    sha256: str
    ofd_locked: bool = False

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor < 0:
            return
        self.descriptor = -1
        os.close(descriptor)


def _open_regular_at(
    parent: _PinnedDirectory,
    name: str,
    *,
    label: str,
    required: bool,
    writable: bool = False,
) -> int | None:
    try:
        descriptor = os.open(
            name,
            (os.O_RDWR if writable else os.O_RDONLY)
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
    except FileNotFoundError:
        if required:
            raise SessionDatabaseMigrationError(f"{label} does not exist")
        return None
    except OSError as exc:
        raise SessionDatabaseMigrationError(f"cannot open {label}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SessionDatabaseMigrationError(f"{label} must be a regular file")
        return descriptor
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _open_legacy_snapshot_files(
    source: Path,
    parent: _PinnedDirectory,
    *,
    require_private: bool = False,
) -> dict[str, _LegacySnapshotFile]:
    files: dict[str, _LegacySnapshotFile] = {}
    try:
        for suffix in ("", "-wal", "-journal", "-shm"):
            name = f"{source.name}{suffix}"
            descriptor = _open_regular_at(
                parent,
                name,
                label=f"legacy session database{suffix}",
                required=not suffix,
                writable=require_private,
            )
            if descriptor is None:
                continue
            try:
                metadata = os.fstat(descriptor)
                if require_private and (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise SessionDatabaseMigrationError(
                        "cannot prove that the legacy session database is private"
                    )
                files[suffix] = _LegacySnapshotFile(
                    name=name,
                    descriptor=descriptor,
                    signature=_file_signature(metadata),
                    sha256=_sha256_descriptor(descriptor),
                )
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
        return files
    except Exception:
        for item in files.values():
            try:
                item.close()
            except OSError:
                pass
        raise


def _validate_legacy_snapshot_files(
    source: Path,
    parent: _PinnedDirectory,
    files: Mapping[str, _LegacySnapshotFile],
) -> None:
    for suffix in ("", "-wal", "-journal", "-shm"):
        expected = files.get(suffix)
        name = f"{source.name}{suffix}"
        try:
            metadata = os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if expected is not None:
                raise SessionDatabaseMigrationError(
                    "legacy session database changed during migration; retry after "
                    "the old T1000 writer exits"
                )
            continue
        if expected is None or stat.S_ISLNK(metadata.st_mode):
            raise SessionDatabaseMigrationError(
                "legacy session database changed during migration; retry after "
                "the old T1000 writer exits"
            )
        if (
            _file_signature(metadata) != expected.signature
            or _file_signature(os.fstat(expected.descriptor)) != expected.signature
            or _sha256_descriptor(expected.descriptor) != expected.sha256
        ):
            raise SessionDatabaseMigrationError(
                "legacy session database changed during migration; retry after "
                "the old T1000 writer exits"
            )


def _process_is_gone_or_zombie(process: Path) -> bool:
    try:
        status_text = (process / "status").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, ProcessLookupError):
        return True
    except OSError:
        return False
    return any(
        line.startswith("State:") and "Z" in line.split()
        for line in status_text.splitlines()
    )


def _proc_process_directories() -> tuple[Path, ...]:
    try:
        return tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "cannot prove that the legacy session database is quiescent"
        ) from exc


def _assert_legacy_snapshot_quiescent(
    files: Mapping[str, _LegacySnapshotFile],
) -> None:
    """Reject migration while another process holds source SQLite files."""

    expected = {
        (item.signature[0], item.signature[1]) for item in files.values()
    }
    expected_locks = {
        (
            int(os.major(item.signature[0])),
            int(os.minor(item.signature[0])),
            int(item.signature[1]),
        )
        for item in files.values()
    }
    try:
        with Path("/proc/locks").open("r", encoding="utf-8") as handle:
            lock_text = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "cannot prove that the legacy session database is quiescent"
        ) from exc
    if len(lock_text) > 4 * 1024 * 1024:
        raise SessionDatabaseMigrationError(
            "cannot prove that the legacy session database is quiescent"
        )
    own_pid = os.getpid()
    for line in lock_text.splitlines():
        fields = line.split()
        lock_pid = None
        if len(fields) >= 5:
            try:
                lock_pid = int(fields[4])
            except ValueError:
                pass
        for field in fields:
            identity = field.split(":")
            if len(identity) != 3:
                continue
            try:
                lock_identity = (
                    int(identity[0], 16),
                    int(identity[1], 16),
                    int(identity[2], 10),
                )
            except ValueError:
                continue
            own_ofd_lock = fields[1:2] == ["OFDLCK"] and any(
                item.ofd_locked
                and (
                    int(os.major(item.signature[0])),
                    int(os.minor(item.signature[0])),
                    int(item.signature[1]),
                )
                == lock_identity
                for item in files.values()
            )
            if (
                lock_identity in expected_locks
                and lock_pid != own_pid
                and not own_ofd_lock
            ):
                raise SessionDatabaseMigrationError(
                    "legacy session database is still open; retry after the old "
                    "T1000 writer exits"
                )
            break
    own_descriptors = {item.descriptor for item in files.values()}
    for process in _proc_process_directories():
        if not process.name.isdecimal():
            continue
        try:
            process_metadata = process.stat()
        except FileNotFoundError:
            continue
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            if _process_is_gone_or_zombie(process):
                continue
            raise SessionDatabaseMigrationError(
                "cannot prove that the legacy session database is quiescent"
            ) from exc
        except OSError as exc:
            raise SessionDatabaseMigrationError(
                "cannot prove that the legacy session database is quiescent"
            ) from exc
        if process_metadata.st_uid != os.geteuid():
            continue
        try:
            descriptors = tuple((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            if _process_is_gone_or_zombie(process):
                continue
            raise SessionDatabaseMigrationError(
                "cannot prove that the legacy session database is quiescent"
            ) from exc
        except OSError as exc:
            raise SessionDatabaseMigrationError(
                "cannot prove that the legacy session database is quiescent"
            ) from exc
        for descriptor_path in descriptors:
            if (
                int(process.name) == own_pid
                and descriptor_path.name.isdecimal()
                and int(descriptor_path.name) in own_descriptors
            ):
                continue
            try:
                metadata = descriptor_path.stat()
            except FileNotFoundError:
                continue
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise SessionDatabaseMigrationError(
                    "cannot prove that the legacy session database is quiescent"
                ) from exc
            if (int(metadata.st_dev), int(metadata.st_ino)) in expected:
                raise SessionDatabaseMigrationError(
                    "legacy session database is still open; retry after the old "
                    "T1000 writer exits"
                )


def _lock_legacy_snapshot_files(
    files: Mapping[str, _LegacySnapshotFile],
) -> None:
    """Hold SQLite-compatible exclusive fences until migration is sealed."""

    if not hasattr(fcntl, "F_OFD_SETLK"):
        raise SessionDatabaseMigrationError(
            "cannot establish an open-file-description migration fence"
        )
    for item in files.values():
        try:
            request = struct.pack(
                "hhqqi",
                fcntl.F_WRLCK,
                os.SEEK_SET,
                0,
                0,
                0,
            )
            fcntl.fcntl(
                item.descriptor,
                fcntl.F_OFD_SETLK,
                request,
            )
            item.ofd_locked = True
        except OSError as exc:
            raise SessionDatabaseMigrationError(
                "legacy session database is still open; retry after the old "
                "T1000 writer exits"
            ) from exc



def _copy_descriptor(descriptor: int, destination: Path) -> None:
    output = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                view = view[written:]
        os.fsync(output)
    finally:
        os.close(output)


def _verify_descriptor_copy(
    source: _LegacySnapshotFile,
    destination: Path,
) -> None:
    descriptor = os.open(
        destination,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_size) != int(source.signature[2])
            or _sha256_descriptor(descriptor) != source.sha256
        ):
            raise SessionDatabaseMigrationError(
                "copied SQLite component failed its descriptor hash proof"
            )
    finally:
        os.close(descriptor)


def _copy_legacy_sqlite_snapshot(
    source: Path,
    source_parent: _PinnedDirectory,
    snapshot_directory: Path,
) -> tuple[Path, dict[str, _LegacySnapshotFile]]:
    files = _open_legacy_snapshot_files(
        source,
        source_parent,
        require_private=True,
    )
    try:
        _assert_legacy_snapshot_quiescent(files)
        _lock_legacy_snapshot_files(files)
        _assert_legacy_snapshot_quiescent(files)
        for suffix in ("", "-wal", "-journal"):
            item = files.get(suffix)
            if item is not None:
                copied = snapshot_directory / item.name
                _copy_descriptor(item.descriptor, copied)
                _verify_descriptor_copy(item, copied)
        _validate_legacy_snapshot_files(source, source_parent, files)
        return snapshot_directory / source.name, files
    except Exception:
        for item in files.values():
            item.close()
        raise


def _harden_regular_file(path: Path, mode: int) -> None:
    try:
        descriptor = _open_sqlite_inode(path)
    except FileNotFoundError:
        return
    try:
        _chmod_sqlite_inode(descriptor, mode, label=str(path))
        if _directory_identity(path.lstat()) != _directory_identity(os.fstat(descriptor)):
            raise SessionDatabaseMigrationError(f"database file identity changed: {path}")
    except StorageFabricError as exc:
        raise SessionDatabaseMigrationError(str(exc)) from exc
    finally:
        os.close(descriptor)


def _harden_database_permissions(path: Path) -> None:
    _harden_regular_file(path, 0o600)
    _harden_regular_file(path.with_name(f"{path.name}-wal"), 0o600)
    _harden_regular_file(path.with_name(f"{path.name}-shm"), 0o600)


def _harden_bound_database_permissions(binding: DatabaseFileBinding) -> None:
    binding.revalidate()
    _chmod_sqlite_inode(binding.descriptor, 0o600, label="database_root")
    for suffix in ("-wal", "-shm"):
        _harden_regular_at(binding.parent, f"{binding.path.name}{suffix}", 0o600)
    binding.revalidate()


def _is_managed_state_path(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(
            managed_state_dir().resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _check_sqlite_database(path: Path, *, immutable: bool = False) -> None:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?{query}",
            uri=True,
            timeout=5.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise SessionDatabaseMigrationError(
            f"{path} is not a readable SQLite database"
        ) from exc
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).casefold() != "ok":
            detail = str(result[0]) if result else "no result"
            raise SessionDatabaseMigrationError(
                f"SQLite integrity check failed for {path}: {detail}"
            )
    except sqlite3.Error as exc:
        raise SessionDatabaseMigrationError(
            f"SQLite integrity check failed for {path}"
        ) from exc
    finally:
        connection.close()


def _normalize_private_sqlite_snapshot(path: Path) -> None:
    """Recover copied WAL/journal state and leave one standalone database."""

    try:
        connection = sqlite3.connect(str(path), timeout=1.0)
    except (OSError, sqlite3.Error) as exc:
        raise SessionDatabaseMigrationError(
            f"{path} is not a readable SQLite database"
        ) from exc
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).casefold() != "ok":
            detail = str(result[0]) if result else "no result"
            raise SessionDatabaseMigrationError(
                f"SQLite integrity check failed for {path}: {detail}"
            )
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).casefold() != "delete":
            raise SessionDatabaseMigrationError(
                "legacy session snapshot could not be made standalone"
            )
        connection.execute("PRAGMA synchronous = FULL")
        connection.commit()
    except sqlite3.Error as exc:
        raise SessionDatabaseMigrationError(
            f"SQLite integrity check failed for {path}"
        ) from exc
    finally:
        connection.close()
    _check_sqlite_database(path, immutable=True)


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise SessionDatabaseMigrationError(
            f"{path} is not a readable SQLite database"
        ) from exc
    connection.row_factory = sqlite3.Row
    return connection


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name != ?
        ORDER BY name
        """,
        (_MIGRATION_STATE_TABLE,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, int], ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return tuple((str(row[1]), int(row[5])) for row in rows)


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _update_content_digest(digest: Any, value: object) -> None:
    if value is None:
        tag = b"n"
        payload = b""
    elif isinstance(value, int):
        tag = b"i"
        payload = str(value).encode("ascii")
    elif isinstance(value, float):
        tag = b"f"
        payload = struct.pack(">d", value)
    elif isinstance(value, str):
        tag = b"s"
        payload = value.encode("utf-8", errors="surrogatepass")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        tag = b"b"
        payload = bytes(value)
    else:
        raise SessionDatabaseMigrationError(
            f"unsupported SQLite value type in migration parity proof: {type(value).__name__}"
        )
    digest.update(tag)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _database_content_sha256(
    connection: sqlite3.Connection,
    tables: Iterable[str],
) -> str:
    digest = hashlib.sha256()
    for table in tables:
        encoded_table = table.encode("utf-8", errors="surrogatepass")
        digest.update(b"t")
        digest.update(len(encoded_table).to_bytes(8, "big"))
        digest.update(encoded_table)
        try:
            row_digests: list[bytes] = []
            rows = connection.execute(
                f"SELECT * FROM {_quote_identifier(table)}"
            )
            for row in rows:
                row_digest = hashlib.sha256()
                row_digest.update(len(row).to_bytes(8, "big"))
                for value in row:
                    _update_content_digest(row_digest, value)
                row_digests.append(row_digest.digest())
            for row_digest in sorted(row_digests):
                digest.update(b"r")
                digest.update(row_digest)
        except sqlite3.Error as exc:
            raise SessionDatabaseMigrationError(
                f"cannot prove row-content parity for SQLite table {table}"
            ) from exc
        digest.update(b"e")
    return digest.hexdigest()


def _database_summary(connection: sqlite3.Connection) -> _DatabaseSummary:
    tables = _database_tables(connection)
    table_counts = tuple((table, _table_count(connection, table)) for table in tables)
    schema_rows = connection.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND name != ?
          AND tbl_name != ?
        ORDER BY type, name, tbl_name, sql
        """,
        (_MIGRATION_STATE_TABLE, _MIGRATION_STATE_TABLE),
    ).fetchall()
    schema_sha256 = hashlib.sha256(
        json.dumps(
            [tuple(str(value) for value in row) for row in schema_rows],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    user_version_row = connection.execute("PRAGMA user_version").fetchone()
    user_version = int(user_version_row[0]) if user_version_row is not None else 0
    counts = dict(table_counts)
    distinct_windows = 0
    if "sessions" in counts and any(
        name == "window_id" for name, _position in _table_columns(connection, "sessions")
    ):
        row = connection.execute(
            "SELECT COUNT(DISTINCT window_id) FROM sessions"
        ).fetchone()
        distinct_windows = int(row[0]) if row is not None else 0
    return _DatabaseSummary(
        schema_sha256=schema_sha256,
        content_sha256=_database_content_sha256(connection, tables),
        user_version=user_version,
        session_count=counts.get("sessions", 0),
        remembered_window_count=counts.get("remembered_windows", 0),
        distinct_window_count=distinct_windows,
        captain_count=counts.get("window_captains", 0),
        total_row_count=sum(counts.values()),
        table_counts=table_counts,
    )


def _database_is_lossless_subset(
    destination: sqlite3.Connection,
    source: sqlite3.Connection,
) -> bool:
    """Allow replacement only when every destination row exists unchanged."""

    destination_tables = _database_tables(destination)
    destination_counts = {
        table: _table_count(destination, table) for table in destination_tables
    }
    if sum(destination_counts.values()) > _MIGRATION_COMPARE_ROW_LIMIT:
        return False
    source_tables = set(_database_tables(source))
    for table in destination_tables:
        row_count = destination_counts[table]
        if table not in source_tables:
            return False
        destination_columns = _table_columns(destination, table)
        source_columns = _table_columns(source, table)
        destination_names = tuple(name for name, _position in destination_columns)
        source_names = {name for name, _position in source_columns}
        if any(name not in source_names for name in destination_names):
            return False
        primary_key = tuple(
            name
            for name, position in sorted(
                destination_columns,
                key=lambda item: item[1] if item[1] else len(destination_columns) + 1,
            )
            if position
        )
        source_primary_key = tuple(
            name
            for name, position in sorted(
                source_columns,
                key=lambda item: item[1] if item[1] else len(source_columns) + 1,
            )
            if position
        )
        if row_count == 0:
            continue
        if not primary_key or primary_key != source_primary_key:
            return False
        columns_sql = ", ".join(_quote_identifier(name) for name in destination_names)
        rows = destination.execute(
            f"SELECT {columns_sql} FROM {_quote_identifier(table)}"
        )
        where = " AND ".join(
            f"{_quote_identifier(name)} IS ?" for name in primary_key
        )
        for row in rows:
            values = {name: row[name] for name in destination_names}
            source_row = source.execute(
                f"SELECT {columns_sql} FROM {_quote_identifier(table)} WHERE {where}",
                tuple(values[name] for name in primary_key),
            ).fetchone()
            if source_row is None or any(
                source_row[name] != values[name] for name in destination_names
            ):
                return False
    return True


def _verify_summary_parity(
    source: _DatabaseSummary,
    destination: _DatabaseSummary,
) -> None:
    if source != destination:
        raise SessionDatabaseMigrationError(
            "managed session database snapshot failed row/window/schema parity"
        )


def _summary_document(summary: _DatabaseSummary) -> dict[str, object]:
    return {
        "schema_sha256": summary.schema_sha256,
        "content_sha256": summary.content_sha256,
        "user_version": summary.user_version,
        "session_count": summary.session_count,
        "remembered_window_count": summary.remembered_window_count,
        "distinct_window_count": summary.distinct_window_count,
        "captain_count": summary.captain_count,
        "total_row_count": summary.total_row_count,
        "table_counts": {name: count for name, count in summary.table_counts},
    }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_summary_document(value: object) -> None:
    if not isinstance(value, dict):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    required = {
        "schema_sha256",
        "content_sha256",
        "user_version",
        "session_count",
        "remembered_window_count",
        "distinct_window_count",
        "captain_count",
        "total_row_count",
        "table_counts",
    }
    if set(value) != required:
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    if not _is_sha256(value["schema_sha256"]) or not _is_sha256(
        value["content_sha256"]
    ):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    count_names = required - {"schema_sha256", "content_sha256", "table_counts"}
    if any(type(value[name]) is not int or int(value[name]) < 0 for name in count_names):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    table_counts = value["table_counts"]
    if not isinstance(table_counts, dict) or any(
        not isinstance(name, str) or type(count) is not int or count < 0
        for name, count in table_counts.items()
    ):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    if int(value["total_row_count"]) != sum(table_counts.values()):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    for summary_name, table_name in (
        ("session_count", "sessions"),
        ("remembered_window_count", "remembered_windows"),
        ("captain_count", "window_captains"),
    ):
        if int(value[summary_name]) != int(table_counts.get(table_name, 0)):
            raise SessionDatabaseMigrationError(
                "managed session migration parity proof is invalid"
            )
    if int(value["distinct_window_count"]) > int(value["session_count"]):
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )


def _validate_receipt_core(
    core: object,
    *,
    source: Path,
    destination: Path,
) -> None:
    if not isinstance(core, dict):
        raise SessionDatabaseMigrationError(
            "managed session migration marker payload is invalid"
        )
    required = {
        "schema",
        "database_id",
        "created_at",
        "source",
        "destination",
        "migration_status",
        "source_raw_main_sha256",
        "source_file_sha256",
        "source_snapshot_sha256",
        "destination_snapshot_sha256",
        "source_summary",
        "destination_summary",
        "prior_destination_snapshot_sha256",
        "backup_path",
    }
    if set(core) != required:
        raise SessionDatabaseMigrationError(
            "managed session migration marker payload is invalid"
        )
    try:
        database_id = str(uuid.UUID(str(core["database_id"])))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SessionDatabaseMigrationError(
            "managed session migration marker payload is invalid"
        ) from exc
    status = core["migration_status"]
    if (
        core["schema"] != _MIGRATION_RECEIPT_SCHEMA
        or database_id != core["database_id"]
        or not isinstance(core["created_at"], str)
        or not core["created_at"]
        or core["source"] != str(source)
        or core["destination"] != str(destination)
        or status not in {"migrated", "repaired_existing"}
    ):
        raise SessionDatabaseMigrationError(
            "managed session migration marker does not match this database"
        )
    for name in (
        "source_raw_main_sha256",
        "source_snapshot_sha256",
        "destination_snapshot_sha256",
    ):
        if not _is_sha256(core[name]):
            raise SessionDatabaseMigrationError(
                "managed session migration marker payload is invalid"
            )
    if core["source_snapshot_sha256"] != core["destination_snapshot_sha256"]:
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    source_files = core["source_file_sha256"]
    if (
        not isinstance(source_files, dict)
        or set(source_files) - {"main", "wal", "journal", "shm"}
        or source_files.get("main") != core["source_raw_main_sha256"]
        or any(not _is_sha256(value) for value in source_files.values())
    ):
        raise SessionDatabaseMigrationError(
            "managed session migration marker payload is invalid"
        )
    _validate_summary_document(core["source_summary"])
    _validate_summary_document(core["destination_summary"])
    if core["source_summary"] != core["destination_summary"]:
        raise SessionDatabaseMigrationError(
            "managed session migration parity proof is invalid"
        )
    prior_digest = core["prior_destination_snapshot_sha256"]
    backup_path = core["backup_path"]
    if status == "migrated":
        valid_prior = prior_digest is None and backup_path is None
    else:
        valid_prior = (
            _is_sha256(prior_digest)
            and isinstance(backup_path, str)
            and Path(backup_path).is_absolute()
        )
    if not valid_prior:
        raise SessionDatabaseMigrationError(
            "managed session migration marker payload is invalid"
        )


def _migration_receipt_name(destination: Path) -> str:
    return f".{destination.name}.migration-receipt.json"


def _receipt_core_bytes(core: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(core),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_document(core: Mapping[str, object]) -> tuple[bytes, str]:
    digest = hashlib.sha256(_receipt_core_bytes(core)).hexdigest()
    document = dict(core)
    document["receipt_sha256"] = digest
    encoded = (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return encoded, digest


def _read_private_at(parent: _PinnedDirectory, name: str) -> bytes | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "managed session migration receipt cannot be opened safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or int(metadata.st_nlink) != 1
            or metadata.st_size > 64 * 1024
        ):
            raise SessionDatabaseMigrationError(
                "managed session migration receipt is unsafe"
            )
        path_metadata = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if (
            (int(path_metadata.st_dev), int(path_metadata.st_ino))
            != (int(metadata.st_dev), int(metadata.st_ino))
        ):
            raise SessionDatabaseMigrationError(
                "managed session migration receipt is unsafe"
            )
        content = bytearray()
        while len(content) <= 64 * 1024:
            chunk = os.read(descriptor, min(8192, 64 * 1024 + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > 64 * 1024:
            raise SessionDatabaseMigrationError(
                "managed session migration receipt is too large"
            )
        final_metadata = os.fstat(descriptor)
        final_path_metadata = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if (
            _file_signature(final_metadata) != _file_signature(metadata)
            or (int(final_path_metadata.st_dev), int(final_path_metadata.st_ino))
            != (int(metadata.st_dev), int(metadata.st_ino))
        ):
            raise SessionDatabaseMigrationError(
                "managed session migration receipt changed while it was read"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _write_private_at(parent: _PinnedDirectory, name: str, content: bytes) -> None:
    descriptor = -1
    temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent.descriptor,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
        )
        os.fsync(parent.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent.descriptor)
        except FileNotFoundError:
            pass


def _unlink_private_at(parent: _PinnedDirectory, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent.descriptor)
    except FileNotFoundError:
        return
    os.fsync(parent.descriptor)


def _validate_bound_path(
    parent: _PinnedDirectory,
    name: str,
    opened_metadata: os.stat_result,
) -> os.stat_result:
    try:
        path_metadata = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "managed session database identity changed during receipt verification"
        ) from exc
    if (
        not stat.S_ISREG(path_metadata.st_mode)
        or (int(path_metadata.st_dev), int(path_metadata.st_ino))
        != (int(opened_metadata.st_dev), int(opened_metadata.st_ino))
    ):
        raise SessionDatabaseMigrationError(
            "managed session database identity changed during receipt verification"
        )
    return path_metadata


def _unlink_bound_path(
    parent: _PinnedDirectory,
    name: str,
    opened_metadata: os.stat_result,
) -> None:
    _validate_bound_path(parent, name, opened_metadata)
    os.unlink(name, dir_fd=parent.descriptor)
    os.fsync(parent.descriptor)


def _clean_migration_link_residue(
    parent: _PinnedDirectory,
    destination: Path,
    descriptor: int,
) -> None:
    opened_metadata = os.fstat(descriptor)
    _validate_bound_path(parent, destination.name, opened_metadata)
    if int(opened_metadata.st_nlink) == 1:
        return
    prefix = f".{destination.name}.migration-"
    for name in os.listdir(parent.descriptor):
        if not name.startswith(prefix):
            continue
        try:
            metadata = os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and (int(metadata.st_dev), int(metadata.st_ino))
            == (int(opened_metadata.st_dev), int(opened_metadata.st_ino))
        ):
            os.unlink(name, dir_fd=parent.descriptor)
    os.fsync(parent.descriptor)
    opened_metadata = os.fstat(descriptor)
    _validate_bound_path(parent, destination.name, opened_metadata)
    if int(opened_metadata.st_nlink) != 1:
        raise SessionDatabaseMigrationError(
            "managed session database has an unsafe hard-link alias"
        )


def _install_migration_marker(
    snapshot: Path,
    *,
    core: Mapping[str, object],
    receipt_sha256: str,
) -> None:
    connection = sqlite3.connect(str(snapshot), timeout=1.0)
    try:
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).casefold() != "delete":
            raise SessionDatabaseMigrationError(
                "managed session migration snapshot could not be made standalone"
            )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_quote_identifier(_MIGRATION_STATE_TABLE)} (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema TEXT NOT NULL,
                database_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL
            )
            """
        )
        connection.execute(f"DELETE FROM {_quote_identifier(_MIGRATION_STATE_TABLE)}")
        connection.execute(
            f"""
            INSERT INTO {_quote_identifier(_MIGRATION_STATE_TABLE)}
                (singleton, schema, database_id, payload_json, receipt_sha256)
            VALUES (1, ?, ?, ?, ?)
            """,
            (
                _MIGRATION_RECEIPT_SCHEMA,
                str(core["database_id"]),
                _receipt_core_bytes(core).decode("utf-8"),
                receipt_sha256,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _check_sqlite_database(snapshot, immutable=True)
    descriptor = os.open(snapshot, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verified_migration_receipt(
    destination: Path,
    source: Path,
    parent: _PinnedDirectory,
) -> SessionDatabaseMigration | None:
    receipt_name = _migration_receipt_name(destination)
    existing_receipt = _read_private_at(parent, receipt_name)
    if existing_receipt is None:
        return None
    descriptor = _open_regular_at(
        parent,
        destination.name,
        label="managed session database",
        required=True,
    )
    assert descriptor is not None
    try:
        _clean_migration_link_residue(parent, destination, descriptor)
        opened_metadata = os.fstat(descriptor)
        _validate_bound_path(parent, destination.name, opened_metadata)
        connection = _open_readonly_database(Path(f"/proc/self/fd/{descriptor}"))
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_MIGRATION_STATE_TABLE,),
            ).fetchone()
            if exists is None:
                raise SessionDatabaseMigrationError(
                    "managed session migration receipt is not bound to the database"
                )
            row = connection.execute(
                f"""
                SELECT schema, database_id, payload_json, receipt_sha256
                FROM {_quote_identifier(_MIGRATION_STATE_TABLE)}
                WHERE singleton = 1
                """
            ).fetchone()
            if row is None or str(row["schema"]) != _MIGRATION_RECEIPT_SCHEMA:
                raise SessionDatabaseMigrationError(
                    "managed session migration marker is invalid"
                )
            try:
                core = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SessionDatabaseMigrationError(
                    "managed session migration marker payload is invalid"
                ) from exc
            _validate_receipt_core(
                core,
                source=source,
                destination=destination,
            )
            assert isinstance(core, dict)
            encoded, digest = _receipt_document(core)
            if (
                digest != str(row["receipt_sha256"])
                or str(row["database_id"]) != str(core.get("database_id", ""))
            ):
                raise SessionDatabaseMigrationError(
                    "managed session migration marker does not match this database"
                )
        finally:
            connection.close()
        _validate_bound_path(parent, destination.name, opened_metadata)
        if existing_receipt != encoded:
            raise SessionDatabaseMigrationError(
                "managed session migration receipt does not match its database marker"
            )
        _validate_bound_path(parent, destination.name, opened_metadata)
        destination_digest = _sha256_descriptor(descriptor)
        if _file_signature(os.fstat(descriptor)) != _file_signature(opened_metadata):
            raise SessionDatabaseMigrationError(
                "managed session database changed during receipt verification"
            )
        _validate_bound_path(parent, destination.name, opened_metadata)
        final_receipt = _read_private_at(parent, receipt_name)
        if final_receipt != encoded:
            raise SessionDatabaseMigrationError(
                "managed session migration receipt changed during verification"
            )
    finally:
        os.close(descriptor)
    receipt_path = destination.parent / receipt_name
    return SessionDatabaseMigration(
        "verified",
        source,
        destination,
        source_sha256=str(core.get("source_raw_main_sha256") or "") or None,
        destination_sha256=destination_digest,
        receipt_path=receipt_path,
        backup_path=Path(str(core["backup_path"])) if core.get("backup_path") else None,
        database_id=str(core.get("database_id") or "") or None,
    )


def _sqlite_backup(source: Path, destination: Path) -> _DatabaseSummary:
    source_connection = _open_readonly_database(source)
    try:
        source_summary = _database_summary(source_connection)
        destination_connection = sqlite3.connect(str(destination), timeout=1.0)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        except sqlite3.Error as exc:
            raise SessionDatabaseMigrationError(
                f"database snapshot failed for {source}"
            ) from exc
        finally:
            destination_connection.close()
    finally:
        source_connection.close()
    os.chmod(destination, 0o600)
    _check_sqlite_database(destination, immutable=True)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    connection = _open_readonly_database(destination)
    try:
        destination_summary = _database_summary(connection)
    finally:
        connection.close()
    _verify_summary_parity(source_summary, destination_summary)
    return source_summary


def _open_exclusive_database(destination: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(str(destination), timeout=0.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
        return connection
    except sqlite3.Error as exc:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        raise SessionDatabaseMigrationError(
            "managed session database changed or became busy during migration"
        ) from exc


def _open_validated_exclusive_database(
    destination: Path,
    parent: _PinnedDirectory,
    files: dict[str, _LegacySnapshotFile],
) -> sqlite3.Connection:
    """Bind only SQLite's empty-WAL ctime touch to its exclusive lock probe.

    All files must pass the original snapshot checks immediately before the
    probe. SQLite may truncate an already empty WAL while acquiring its lock,
    changing ctime without changing mtime or bytes. Revalidate every component
    with that one new ctime, then retain the full signature for publication.
    """

    wal = files.get("-wal")
    wal_before = os.fstat(wal.descriptor) if wal is not None else None
    _validate_legacy_snapshot_files(destination, parent, files)
    connection = _open_exclusive_database(parent.proc_path / destination.name)
    try:
        locking_mode = connection.execute("PRAGMA locking_mode").fetchone()
        if locking_mode is None or str(locking_mode[0]).casefold() != "exclusive":
            raise SessionDatabaseMigrationError(
                "managed session database exclusive lock was not established"
            )
        validated_files = dict(files)
        if wal is not None and wal.signature[2] == 0:
            assert wal_before is not None
            wal_after = os.fstat(wal.descriptor)
            signature = _file_signature(wal_after)
            if (
                _file_signature(wal_before) != wal.signature
                or signature[:4] != wal.signature[:4]
                or signature[4] < wal.signature[4]
                or wal_before.st_uid != os.geteuid()
                or stat.S_IMODE(wal_before.st_mode) != 0o600
                or wal_before.st_nlink != 1
                or (
                    wal_before.st_mode,
                    wal_before.st_uid,
                    wal_before.st_gid,
                    wal_before.st_nlink,
                ) != (
                    wal_after.st_mode,
                    wal_after.st_uid,
                    wal_after.st_gid,
                    wal_after.st_nlink,
                )
            ):
                raise SessionDatabaseMigrationError(
                    "managed session database WAL changed during exclusive locking"
                )
            validated_files["-wal"] = replace(wal, signature=signature)
        _validate_legacy_snapshot_files(destination, parent, validated_files)
        if wal is not None:
            wal.signature = validated_files["-wal"].signature
        return connection
    except Exception as exc:
        connection.close()
        if isinstance(exc, sqlite3.Error):
            raise SessionDatabaseMigrationError(
                "managed session database changed or became busy during migration"
            ) from exc
        raise


def _replace_database_from_snapshot(
    source: Path,
    destination_connection: sqlite3.Connection,
) -> _DatabaseSummary:
    """Transactionally replace a quiescent SQLite database from a snapshot."""

    source_connection = _open_readonly_database(source)
    try:
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            journal_mode = destination_connection.execute(
                "PRAGMA journal_mode = DELETE"
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise SessionDatabaseMigrationError(
                    "managed session database could not be made standalone"
                )
            destination_connection.execute("PRAGMA synchronous = FULL")
            destination_connection.commit()
        except sqlite3.Error as exc:
            raise SessionDatabaseMigrationError(
                "managed session database changed or became busy during migration"
            ) from exc
    finally:
        source_connection.close()
    source_connection = _open_readonly_database(source)
    try:
        source_summary = _database_summary(source_connection)
    finally:
        source_connection.close()
    destination_summary = _database_summary(destination_connection)
    _verify_summary_parity(source_summary, destination_summary)
    return destination_summary


def _open_migration_lock(parent: _PinnedDirectory, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            0o600,
            dir_fd=parent.descriptor,
        )
    except OSError as exc:
        raise SessionDatabaseMigrationError("cannot create migration lock") from exc
    try:
        os.fchmod(descriptor, 0o600)
        _validate_migration_lock(parent, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_migration_lock(
    parent: _PinnedDirectory,
    name: str,
    descriptor: int,
) -> None:
    try:
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise SessionDatabaseMigrationError("migration lock is unsafe") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or int(metadata.st_nlink) != 1
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(path_metadata.st_dev), int(path_metadata.st_ino))
    ):
        raise SessionDatabaseMigrationError("migration lock is unsafe")


def _create_backup_directory(
    parent: _PinnedDirectory,
    destination: Path,
) -> tuple[_PinnedDirectory, Path]:
    name = f".{destination.name}.pre-migration-{uuid.uuid4().hex}"
    try:
        os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent.descriptor,
        )
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "cannot create managed session database backup"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    backup_path = destination.parent / name
    return (
        _PinnedDirectory(
            path=backup_path,
            descriptor=descriptor,
            device=parent.device,
            identity=(int(metadata.st_dev), int(metadata.st_ino)),
        ),
        backup_path,
    )


def _remove_backup_directory(
    parent: _PinnedDirectory,
    backup: _PinnedDirectory,
) -> None:
    for name in (
        "original.sqlite3",
        "original.sqlite3-wal",
        "original.sqlite3-journal",
        "original.sqlite3-shm",
        "logical.sqlite3",
        "logical.sqlite3-wal",
        "logical.sqlite3-journal",
        "logical.sqlite3-shm",
    ):
        try:
            os.unlink(name, dir_fd=backup.descriptor)
        except FileNotFoundError:
            pass
    name = backup.path.name
    backup.close()
    try:
        os.rmdir(name, dir_fd=parent.descriptor)
    except FileNotFoundError:
        pass


def _migration_target_parent(target: Path) -> _PinnedDirectory:
    try:
        production_parent = pin_production_database_parent(target)
        if production_parent is not None:
            return production_parent
        return _open_pinned_directory(
            target.parent,
            create=True,
            require_owner=True,
            final_mode=0o700,
        )
    except StorageFabricError as exc:
        raise SessionDatabaseMigrationError(
            f"cannot bind managed session database parent: {exc}"
        ) from exc


def _migration_source_parent(source: Path) -> _PinnedDirectory:
    try:
        parent = _open_pinned_directory(source.parent, create=False)
    except StorageFabricError as exc:
        raise SessionDatabaseMigrationError(
            f"cannot bind legacy session database parent: {exc}"
        ) from exc
    metadata = os.fstat(parent.descriptor)
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        parent.close()
        raise SessionDatabaseMigrationError(
            "cannot prove that the legacy session database parent is private"
        )
    return parent


def _harden_regular_at(parent: _PinnedDirectory, name: str, mode: int) -> None:
    try:
        descriptor = _open_sqlite_inode(name, dir_fd=parent.descriptor)
    except FileNotFoundError:
        return
    try:
        _chmod_sqlite_inode(descriptor, mode, label=f"managed database file {name}")
        _validate_bound_path(parent, name, os.fstat(descriptor))
    except StorageFabricError as exc:
        raise SessionDatabaseMigrationError(str(exc)) from exc
    finally:
        os.close(descriptor)


def _harden_database_at(parent: _PinnedDirectory, name: str) -> None:
    _harden_regular_at(parent, name, 0o600)
    _harden_regular_at(parent, f"{name}-wal", 0o600)
    _harden_regular_at(parent, f"{name}-shm", 0o600)


def migrate_legacy_database(
    destination: str | Path | None = None,
    *,
    legacy_path: str | Path | None = None,
) -> SessionDatabaseMigration:
    """Publish a parity-proven SSD copy without mutating the legacy registry.

    A previously published database is accepted only when its embedded receipt
    verifies.  An unsealed destination may be repaired only when every one of
    its durable rows exists unchanged in a quiescent legacy snapshot.  This
    permits recovery from the known empty/outdated SSD bootstrap without ever
    overwriting divergent managed state.
    """

    source = _absolute_migration_path(
        legacy_path
        if legacy_path is not None
        else legacy_state_dir() / "sessions.sqlite3",
        label="legacy session database",
    )
    target = _migration_file(
        destination if destination is not None else default_db_path(),
        label="managed session database",
    )
    if source == target:
        raise SessionDatabaseMigrationError(
            "legacy and managed session database paths must be distinct"
        )
    try:
        if source.exists() and target.exists() and os.path.samefile(source, target):
            raise SessionDatabaseMigrationError(
                "legacy and managed session database files must be distinct"
            )
    except OSError as exc:
        raise SessionDatabaseMigrationError(
            "cannot compare legacy and managed session database identities"
        ) from exc

    target_parent = _migration_target_parent(target)
    source_parent: _PinnedDirectory | None = None
    source_files: dict[str, _LegacySnapshotFile] | None = None
    target_files: dict[str, _LegacySnapshotFile] | None = None
    backup: _PinnedDirectory | None = None
    backup_retained = False
    target_connection: sqlite3.Connection | None = None
    temporary_basename: str | None = None
    lock_name = f".{target.name}.migration.lock"
    try:
        lock_fd = _open_migration_lock(target_parent, lock_name)
        with os.fdopen(lock_fd, "r+b", closefd=True) as lock_handle:
            try:
                fcntl.flock(
                    lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise SessionDatabaseMigrationInProgress(
                    "MIGRATION_IN_PROGRESS: another primary T1000 process is "
                    "publishing the managed session database"
                ) from exc
            _validate_migration_lock(
                target_parent,
                lock_name,
                lock_handle.fileno(),
            )

            existing_receipt = _read_private_at(
                target_parent,
                _migration_receipt_name(target),
            )
            target_descriptor = _open_regular_at(
                target_parent,
                target.name,
                label="managed session database",
                required=False,
            )
            target_exists = target_descriptor is not None
            if not target_exists and existing_receipt is not None:
                raise SessionDatabaseMigrationError(
                    "managed session database is missing but its migration receipt remains"
                )
            if target_descriptor is not None:
                try:
                    _clean_migration_link_residue(
                        target_parent,
                        target,
                        target_descriptor,
                    )
                    _check_sqlite_database(target_parent.proc_path / target.name)
                    verified = _verified_migration_receipt(
                        target,
                        source,
                        target_parent,
                    )
                    if verified is not None:
                        _harden_database_at(target_parent, target.name)
                        return verified
                finally:
                    os.close(target_descriptor)

            source = _migration_file(source, label="legacy session database")
            try:
                source.lstat()
            except FileNotFoundError:
                if target_exists:
                    raise SessionDatabaseMigrationError(
                        "unverified managed session database cannot be accepted "
                        "without the legacy source"
                    )
                return SessionDatabaseMigration("source_missing", source, target)
            except OSError as exc:
                raise SessionDatabaseMigrationError(
                    "cannot inspect legacy session database"
                ) from exc

            source_parent = _migration_source_parent(source)
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.migration-",
                dir=target_parent.proc_path,
            )
            os.fchmod(temporary_fd, 0o600)
            os.close(temporary_fd)
            temporary = Path(temporary_name)
            temporary_basename = temporary.name

            with tempfile.TemporaryDirectory(
                prefix=f".{target.name}.legacy-snapshot-",
                dir=target_parent.proc_path,
            ) as snapshot_name:
                snapshot, source_files = _copy_legacy_sqlite_snapshot(
                    source,
                    source_parent,
                    Path(snapshot_name),
                )
                _assert_legacy_snapshot_quiescent(source_files)
                _normalize_private_sqlite_snapshot(snapshot)
                source_summary = _sqlite_backup(snapshot, temporary)
                _validate_legacy_snapshot_files(source, source_parent, source_files)
                _assert_legacy_snapshot_quiescent(source_files)

            source_digest = source_files[""].sha256
            source_file_digests = {
                "main" if not suffix else suffix[1:]: item.sha256
                for suffix, item in source_files.items()
            }
            source_snapshot_digest = _sha256_file(temporary)
            backup_path: Path | None = None
            backup_logical: Path | None = None
            prior_target_digest: str | None = None
            prior_target_summary: _DatabaseSummary | None = None

            if target_exists:
                target_files = _open_legacy_snapshot_files(target, target_parent)
                _assert_legacy_snapshot_quiescent(target_files)
                backup, backup_path = _create_backup_directory(target_parent, target)
                os.fsync(target_parent.descriptor)
                backup_root = backup.proc_path
                for suffix in ("", "-wal", "-journal"):
                    item = target_files.get(suffix)
                    if item is None:
                        continue
                    copied = backup_root / f"original.sqlite3{suffix}"
                    _copy_descriptor(
                        item.descriptor,
                        copied,
                    )
                    _verify_descriptor_copy(item, copied)
                backup_logical = backup_root / "logical.sqlite3"
                with tempfile.TemporaryDirectory(
                    prefix=".logical-snapshot-",
                    dir=backup_root,
                ) as target_snapshot_name:
                    working = Path(target_snapshot_name) / "original.sqlite3"
                    for suffix in ("", "-wal", "-journal"):
                        item = target_files.get(suffix)
                        if item is not None:
                            copied = working.with_name(f"{working.name}{suffix}")
                            _copy_descriptor(
                                item.descriptor,
                                copied,
                            )
                            _verify_descriptor_copy(item, copied)
                    _normalize_private_sqlite_snapshot(working)
                    prior_target_summary = _sqlite_backup(working, backup_logical)
                os.fsync(backup.descriptor)
                os.fsync(target_parent.descriptor)
                prior_target_digest = _sha256_file(backup_logical)
                destination_connection = _open_readonly_database(backup_logical)
                source_connection = _open_readonly_database(temporary)
                try:
                    if not _database_is_lossless_subset(
                        destination_connection,
                        source_connection,
                    ):
                        raise SessionDatabaseMigrationError(
                            "managed session database diverges from the legacy "
                            "registry; refusing automatic replacement"
                        )
                finally:
                    source_connection.close()
                    destination_connection.close()
                _validate_legacy_snapshot_files(target, target_parent, target_files)
                _assert_legacy_snapshot_quiescent(target_files)
                target_connection = _open_validated_exclusive_database(
                    target,
                    target_parent,
                    target_files,
                )
                _validate_bound_path(
                    target_parent,
                    target.name,
                    os.fstat(target_files[""].descriptor),
                )
                assert prior_target_summary is not None
                _verify_summary_parity(
                    prior_target_summary,
                    _database_summary(target_connection),
                )

            database_id = str(uuid.uuid4())
            migration_status = "repaired_existing" if target_exists else "migrated"
            core: dict[str, object] = {
                "schema": _MIGRATION_RECEIPT_SCHEMA,
                "database_id": database_id,
                "created_at": utc_now(),
                "source": str(source),
                "destination": str(target),
                "migration_status": migration_status,
                "source_raw_main_sha256": source_digest,
                "source_file_sha256": source_file_digests,
                "source_snapshot_sha256": source_snapshot_digest,
                "destination_snapshot_sha256": source_snapshot_digest,
                "source_summary": _summary_document(source_summary),
                "destination_summary": _summary_document(source_summary),
                "prior_destination_snapshot_sha256": prior_target_digest,
                "backup_path": str(backup_path) if backup_path is not None else None,
            }
            receipt_bytes, receipt_digest = _receipt_document(core)
            _install_migration_marker(
                temporary,
                core=core,
                receipt_sha256=receipt_digest,
            )
            migrated_connection = _open_readonly_database(temporary)
            try:
                _verify_summary_parity(
                    source_summary,
                    _database_summary(migrated_connection),
                )
            finally:
                migrated_connection.close()

            _validate_legacy_snapshot_files(source, source_parent, source_files)
            _assert_legacy_snapshot_quiescent(source_files)
            published_descriptor: int | None = None
            published_metadata: os.stat_result | None = None
            published_new = False
            target_replaced = False
            publication_committed = False
            try:
                if target_files is not None:
                    _validate_legacy_snapshot_files(
                        target,
                        target_parent,
                        target_files,
                    )
                    assert target_connection is not None
                    assert backup_logical is not None
                    backup_retained = True
                    target_replaced = True
                    _replace_database_from_snapshot(temporary, target_connection)
                    _validate_bound_path(
                        target_parent,
                        target.name,
                        os.fstat(target_files[""].descriptor),
                    )
                else:
                    try:
                        os.link(
                            temporary_basename,
                            target.name,
                            src_dir_fd=target_parent.descriptor,
                            dst_dir_fd=target_parent.descriptor,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise SessionDatabaseMigrationError(
                            "managed session database appeared during migration; retry"
                        ) from exc
                    published_new = True
                    published_descriptor = _open_regular_at(
                        target_parent,
                        target.name,
                        label="managed session database",
                        required=True,
                    )
                    assert published_descriptor is not None
                    published_metadata = os.fstat(published_descriptor)
                    _validate_bound_path(
                        target_parent,
                        target.name,
                        published_metadata,
                    )
                    os.unlink(temporary_basename, dir_fd=target_parent.descriptor)
                    temporary_basename = None
                    os.fsync(target_parent.descriptor)

                _validate_legacy_snapshot_files(source, source_parent, source_files)
                _assert_legacy_snapshot_quiescent(source_files)
                if target_connection is not None:
                    published_summary = _database_summary(target_connection)
                else:
                    _check_sqlite_database(target_parent.proc_path / target.name)
                    published_connection = _open_readonly_database(
                        target_parent.proc_path / target.name
                    )
                    try:
                        published_summary = _database_summary(published_connection)
                    finally:
                        published_connection.close()
                _verify_summary_parity(source_summary, published_summary)

                if published_descriptor is None:
                    published_descriptor = _open_regular_at(
                        target_parent,
                        target.name,
                        label="managed session database",
                        required=True,
                    )
                    assert published_descriptor is not None
                    published_metadata = os.fstat(published_descriptor)
                assert published_metadata is not None
                _validate_bound_path(
                    target_parent,
                    target.name,
                    published_metadata,
                )
                os.fsync(published_descriptor)
                target_digest = _sha256_descriptor(published_descriptor)
                os.fchmod(published_descriptor, 0o600)
                _harden_regular_at(target_parent, f"{target.name}-wal", 0o600)
                _harden_regular_at(target_parent, f"{target.name}-shm", 0o600)
                _validate_legacy_snapshot_files(source, source_parent, source_files)
                _assert_legacy_snapshot_quiescent(source_files)
                _validate_bound_path(
                    target_parent,
                    target.name,
                    published_metadata,
                )
                if target_files is not None:
                    _validate_bound_path(
                        target_parent,
                        target.name,
                        os.fstat(target_files[""].descriptor),
                    )
                _write_private_at(
                    target_parent,
                    _migration_receipt_name(target),
                    receipt_bytes,
                )
                publication_committed = True
            except Exception:
                if not publication_committed:
                    if target_replaced:
                        assert target_connection is not None
                        assert backup_logical is not None
                        try:
                            _replace_database_from_snapshot(
                                backup_logical,
                                target_connection,
                            )
                        except Exception:
                            backup_retained = True
                            raise
                    elif published_new and published_metadata is not None:
                        _unlink_bound_path(
                            target_parent,
                            target.name,
                            published_metadata,
                        )
                raise
            finally:
                if published_descriptor is not None:
                    os.close(published_descriptor)
            if target_connection is not None:
                target_connection.close()
                target_connection = None
            verified = _verified_migration_receipt(target, source, target_parent)
            if verified is None:
                raise SessionDatabaseMigrationError(
                    "managed session migration receipt was not published"
                )
            return SessionDatabaseMigration(
                migration_status,
                source,
                target,
                source_sha256=source_digest,
                destination_sha256=target_digest,
                receipt_path=verified.receipt_path,
                backup_path=backup_path,
                database_id=database_id,
            )
    except SessionDatabaseMigrationError:
        raise
    except (OSError, sqlite3.Error, StorageFabricError) as exc:
        raise SessionDatabaseMigrationError(
            f"legacy session database migration failed: {exc}"
        ) from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_files is not None:
            for item in source_files.values():
                item.close()
        if target_files is not None:
            for item in target_files.values():
                item.close()
        if temporary_basename is not None:
            try:
                os.unlink(temporary_basename, dir_fd=target_parent.descriptor)
            except FileNotFoundError:
                pass
        if backup is not None:
            if backup_retained:
                backup.close()
            else:
                _remove_backup_directory(target_parent, backup)
        if source_parent is not None:
            source_parent.close()
        target_parent.close()


class SessionStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        auth_root: str | Path | None = None,
        account_source: str | Path | None = None,
        provision_auth_scopes: bool | None = None,
    ):
        self.path = (
            Path(path).expanduser()
            if path is not None
            else default_db_path()
        )
        self._managed_state = _is_managed_state_path(self.path)
        self.auth_root = (
            Path(auth_root).expanduser()
            if auth_root is not None
            else (
                default_auth_root()
                if path is None
                else self.path.parent / "terminal-auth"
            )
        )
        # Account isolation is an optional integration, not a prerequisite for
        # creating, opening, or recovering a terminal.  Explicit auth paths keep
        # the historical opt-in behavior for account-specific callers/tests; the
        # normal GUI/store path remains completely tool-agnostic.
        self._account_source_override = (
            Path(account_source).expanduser()
            if account_source is not None
            else None
        )
        configured_source = os.environ.get("TERMDECK_CODEX_ACCOUNT_SOURCE", "").strip()
        self.account_source = self._account_source_override or (
            Path(configured_source).expanduser()
            if configured_source
            else Path.home() / ".codex"
        )
        self.provision_auth_scopes = (
            bool(auth_root is not None or account_source is not None)
            if provision_auth_scopes is None
            else bool(provision_auth_scopes)
        )
        self._database_binding: DatabaseFileBinding | None = None
        if self.path != Path(":memory:"):
            self._database_binding = pin_production_database(self.path)
            if self._database_binding is not None:
                self.path = self._database_binding.path
                _harden_bound_database_permissions(self._database_binding)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self._managed_state:
                    managed_root = managed_state_dir()
                    managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    managed_root.chmod(0o700)
                    self.path.parent.chmod(0o700)
                    _harden_database_permissions(self.path)
        if self.provision_auth_scopes:
            self.auth_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.auth_root.chmod(0o700)
        # Keep the GUI responsive if a second TermDeck/CLI process briefly
        # holds the registry, and avoid a full fsync for every tab-state update.
        sqlite_path = (
            self._database_binding.sqlite_path
            if self._database_binding is not None
            else str(self.path)
        )
        try:
            self.conn = sqlite3.connect(sqlite_path, timeout=1.0)
            if self._database_binding is not None:
                self._database_binding.revalidate()
            self.conn.row_factory = sqlite3.Row
            self._configure_connection()
            self._init_schema()
            if self._database_binding is not None:
                self._database_binding.revalidate()
                _harden_bound_database_permissions(self._database_binding)
            elif self._managed_state:
                _harden_database_permissions(self.path)
        except Exception:
            connection = getattr(self, "conn", None)
            try:
                if connection is not None:
                    connection.close()
            finally:
                if self._database_binding is not None:
                    self._database_binding.close()
                    self._database_binding = None
            raise

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA busy_timeout = 1000")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        if self.path == Path(":memory:"):
            return
        try:
            mode = self.conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode and str(mode[0]).casefold() == "wal":
                # WAL + NORMAL preserves database consistency while avoiding
                # a storage flush on every small UI metadata transaction.
                self.conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            # Some unusual filesystems cannot host WAL sidecars. SQLite's
            # default journal remains a safe compatibility fallback.
            pass

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            if self._database_binding is not None:
                self._database_binding.close()
                self._database_binding = None

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tag TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                tmux_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                last_pid INTEGER,
                window_id TEXT NOT NULL DEFAULT 'main',
                window_title TEXT NOT NULL DEFAULT 'Main',
                tab_index INTEGER NOT NULL DEFAULT 0,
                visible INTEGER NOT NULL DEFAULT 1,
                pinned INTEGER NOT NULL DEFAULT 0,
                pane_target TEXT,
                external INTEGER NOT NULL DEFAULT 0,
                recovery_command TEXT,
                recovery_cwd TEXT,
                auth_scope_id TEXT,
                codex_home TEXT,
                selected_account TEXT,
                pending_account TEXT,
                auth_restart_required INTEGER NOT NULL DEFAULT 0,
                auth_changed_at TEXT,
                recovery_recipe TEXT,
                recovery_recipe_sha256 TEXT,
                restart_policy TEXT NOT NULL DEFAULT 'on-failure',
                recovery_attempts INTEGER NOT NULL DEFAULT 0,
                recovery_last_attempt_at TEXT,
                recovery_last_error TEXT,
                recovery_intent TEXT NOT NULL DEFAULT 'legacy',
                automatic_name INTEGER CHECK (automatic_name IN (0, 1)),
                recovery_authority_context TEXT,
                recovery_authority_context_sha256 TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS window_captains (
                window_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remembered_windows (
                window_id TEXT PRIMARY KEY,
                window_title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL
            )
            """
        )
        self._ensure_column("window_id", "TEXT NOT NULL DEFAULT 'main'")
        self._ensure_column("window_title", "TEXT NOT NULL DEFAULT 'Main'")
        self._ensure_column("tab_index", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("visible", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("pinned", "INTEGER NOT NULL DEFAULT 0")
        # Leave historical rows NULL: old registries did not distinguish a
        # default title from a manual title equal to the directory basename.
        self._ensure_column("automatic_name", "INTEGER CHECK (automatic_name IN (0, 1))")
        self._ensure_column("pane_target", "TEXT")
        self._ensure_column("external", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("recovery_command", "TEXT")
        self._ensure_column("recovery_cwd", "TEXT")
        self._ensure_column("auth_scope_id", "TEXT")
        self._ensure_column("codex_home", "TEXT")
        self._ensure_column("selected_account", "TEXT")
        self._ensure_column("pending_account", "TEXT")
        self._ensure_column("auth_restart_required", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("auth_changed_at", "TEXT")
        self._ensure_column("recovery_recipe", "TEXT")
        self._ensure_column("recovery_recipe_sha256", "TEXT")
        # No historical context backfill: a recipe/header cannot establish
        # original home, SQLite or qualified effective-launcher authority.
        self._ensure_column("recovery_authority_context", "TEXT")
        self._ensure_column("recovery_authority_context_sha256", "TEXT")
        self._ensure_column(
            "restart_policy",
            "TEXT NOT NULL DEFAULT 'on-failure'",
        )
        self._ensure_column("recovery_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("recovery_last_attempt_at", "TEXT")
        self._ensure_column("recovery_last_error", "TEXT")
        self._ensure_column(
            "recovery_intent",
            "TEXT NOT NULL DEFAULT 'legacy'",
        )
        self._clear_managed_server_local_pane_targets()
        self._backfill_recovery_intents()
        self._backfill_recovery_recipes()
        if self.provision_auth_scopes:
            self._ensure_auth_scopes()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tag ON sessions(tag)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_window ON sessions(window_id, tab_index)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_visible ON sessions(visible)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pinned ON sessions(pinned)")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_auth_scope ON sessions(auth_scope_id) "
            "WHERE auth_scope_id IS NOT NULL AND auth_scope_id != ''"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_codex_home ON sessions(codex_home) "
            "WHERE codex_home IS NOT NULL AND codex_home != ''"
        )
        self._bootstrap_remembered_windows()
        self._cleanup_stale_window_captains()
        self.conn.commit()

    def _backfill_recovery_intents(self) -> int:
        """Convert status-only rows to an explicit safe recovery intent.

        The migration only fills legacy/unknown values.  An explicit active or
        inactive decision made by a newer writer is never overwritten while a
        second process opens the database.
        """

        rows = self.conn.execute(
            "SELECT id, status, recovery_intent FROM sessions"
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            current = normalise_recovery_intent(row["recovery_intent"])
            raw = str(row["recovery_intent"] or "").strip().casefold()
            if raw in {
                RECOVERY_INTENT_ACTIVE,
                RECOVERY_INTENT_INACTIVE,
            }:
                continue
            inferred = recovery_intent_for_status(row["status"])
            if current == inferred and raw == RECOVERY_INTENT_LEGACY and inferred == RECOVERY_INTENT_LEGACY:
                # Ambiguous rows (notably start-error) intentionally remain
                # legacy so startup cannot infer permission from status alone.
                continue
            updates.append((inferred, str(row["id"])))
        if not updates:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            UPDATE sessions
            SET recovery_intent = ?
            WHERE id = ?
              AND lower(trim(COALESCE(recovery_intent, ''))) NOT IN (?, ?)
            """,
            [
                (intent, session_id, RECOVERY_INTENT_ACTIVE, RECOVERY_INTENT_INACTIVE)
                for intent, session_id in updates
            ],
        )
        return max(0, int(self.conn.total_changes - before))

    def _ensure_column(self, name: str, ddl: str) -> None:
        rows = self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        existing = {str(row[1]) for row in rows}
        if name not in existing:
            self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")

    def _clear_managed_server_local_pane_targets(self) -> int:
        """Remove tmux-server-local pane IDs from durable managed records.

        Explicit ``%pane`` targets are valid for adopted external sessions, but
        tmux allocates new IDs after a server restart.  Managed rows are addressed
        by their stable session name and must never carry an old generation's ID.
        The migration is idempotent and intentionally leaves external rows intact.
        """

        cursor = self.conn.execute(
            """
            UPDATE sessions
            SET pane_target = NULL
            WHERE external = 0 AND pane_target IS NOT NULL
            """
        )
        return max(0, int(cursor.rowcount))

    def _bootstrap_remembered_windows(self) -> int:
        """Add only currently open legacy windows to the independent registry.

        Older databases inferred window existence from visible tab rows.  Do
        not resurrect every historical all-hidden group during migration; only
        groups that were still marked open become durable windows.
        """

        rows = self.conn.execute(
            """
            SELECT window_id, window_title, MIN(created_at), MAX(last_opened_at)
            FROM sessions
            WHERE visible = 1 AND status != 'archived'
            GROUP BY window_id, window_title
            """
        ).fetchall()
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO remembered_windows (
                window_id, window_title, created_at, last_opened_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    str(row["window_id"]),
                    str(row["window_title"] or "Window"),
                    str(row[2] or utc_now()),
                    str(row[3] or utc_now()),
                )
                for row in rows
            ],
        )
        return max(0, self.conn.total_changes - before)

    def remember_window(self, window_id: str, window_title: str) -> None:
        """Persist a logical window independently of its current tab states."""

        clean_id = (window_id or "main").strip() or "main"
        clean_title = (window_title or "Window").strip() or "Window"
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO remembered_windows (
                window_id, window_title, created_at, last_opened_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(window_id) DO UPDATE SET
                window_title = excluded.window_title,
                last_opened_at = excluded.last_opened_at
            """,
            (clean_id, clean_title, now, now),
        )
        self.conn.commit()

    def remembered_windows(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT window_id, window_title
            FROM remembered_windows
            ORDER BY window_title COLLATE NOCASE, window_id
            """
        ).fetchall()
        return [(str(row["window_id"]), str(row["window_title"])) for row in rows]

    def forget_window(self, window_id: str) -> bool:
        """Remove an empty logical window from durable restore state.

        Refuse while any session row still belongs to the window.  This keeps a
        partial destructive action (for example, an auth-guarded session that
        could not be stopped) recoverable instead of orphaning it from startup.
        """

        clean_id = (window_id or "main").strip() or "main"
        remaining = self.conn.execute(
            "SELECT 1 FROM sessions WHERE window_id = ? LIMIT 1",
            (clean_id,),
        ).fetchone()
        if remaining is not None:
            return False
        self.conn.execute(
            "DELETE FROM window_captains WHERE window_id = ?",
            (clean_id,),
        )
        self.conn.execute(
            "DELETE FROM remembered_windows WHERE window_id = ?",
            (clean_id,),
        )
        self.conn.commit()
        return True

    def _new_auth_scope(self, *, private_history: bool = False) -> tuple[str, str, str | None]:
        # Keep the optional account integration out of SessionStore's import and
        # normal initialization path.  A missing third-party executable does not
        # affect the generic session registry.
        from .codex_auth import (
            new_auth_scope_id,
            prepare_auth_scope,
            read_auth_state,
        )

        self.auth_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_root.chmod(0o700)
        scope_id = new_auth_scope_id()
        codex_home = self.auth_root / scope_id
        prepare_auth_scope(
            codex_home,
            account_source=self._account_source_override,
            **({"private_history": True} if private_history else {}),
        )
        state = read_auth_state(codex_home, account_source=self.account_source)
        inherited_account = (
            state.selected_account if state.canonical_binding_ready else None
        )
        return scope_id, str(codex_home), inherited_account

    def _ensure_auth_scopes(self) -> None:
        from .codex_auth import prepare_auth_scope

        rows = self.conn.execute(
            "SELECT id, auth_scope_id, codex_home FROM sessions"
        ).fetchall()
        for row in rows:
            scope_id = str(row["auth_scope_id"] or "").strip()
            codex_home = str(row["codex_home"] or "").strip()
            if not scope_id or not codex_home:
                scope_id, codex_home, inherited_account = self._new_auth_scope()
                self.conn.execute(
                    """
                    UPDATE sessions
                    SET auth_scope_id = ?, codex_home = ?, selected_account = ?
                    WHERE id = ?
                    """,
                    (scope_id, codex_home, inherited_account, row["id"]),
                )
            else:
                prepare_auth_scope(
                    codex_home,
                    account_source=self._account_source_override,
                )

    def ensure_auth_scope(self, session_id: str, *, private_history: bool = False) -> SessionRecord:
        """Provision the optional private account environment for one terminal."""

        current = self.get(session_id)
        if current is None:
            raise KeyError(session_id)
        if current.external:
            raise ValueError("external tmux sessions cannot receive a private auth scope")
        if current.auth_scope_id and current.codex_home:
            if private_history:
                from .codex_auth import prepare_auth_scope
                prepare_auth_scope(current.codex_home, account_source=self._account_source_override,
                                   private_history=True)
            return current
        scope_id, codex_home, inherited_account = (
            self._new_auth_scope(private_history=True) if private_history else self._new_auth_scope()
        )
        self.conn.execute(
            """
            UPDATE sessions
            SET auth_scope_id = ?, codex_home = ?, selected_account = ?,
                auth_restart_required = 0
            WHERE id = ? AND (auth_scope_id IS NULL OR auth_scope_id = '')
              AND (codex_home IS NULL OR codex_home = '')
            """,
            (scope_id, codex_home, inherited_account, session_id),
        )
        self.conn.commit()
        refreshed = self.get(session_id)
        if refreshed is None:
            raise KeyError(session_id)
        # A concurrent same-user provisioner may have won; its immutable scope
        # is authoritative.  Remove only our unused empty scope when possible.
        if refreshed.auth_scope_id != scope_id:
            try:
                import shutil

                shutil.rmtree(codex_home)
            except OSError:
                pass
        return refreshed

    @staticmethod
    def _recipe_values_for_record(record: SessionRecord) -> tuple[str | None, str | None, str]:
        encoded = getattr(record, "recovery_recipe", None)
        digest = getattr(record, "recovery_recipe_sha256", None)
        if encoded is not None or digest is not None:
            if encoded is None or digest is None:
                raise SessionRecipeError("session recipe and digest must be stored together")
            recipe = parse_session_recipe(encoded, digest)
            return encoded, digest, recipe.restart_policy
        if bool(getattr(record, "external", False)):
            return None, None, "never"
        return record_recipe_fields(
            command=record.command,
            cwd=record.cwd,
            recovery_command=record.recovery_command,
            recovery_cwd=record.recovery_cwd,
            restart_policy=(
                getattr(record, "restart_policy", DEFAULT_RESTART_POLICY)
                or DEFAULT_RESTART_POLICY
            ),
            origin=(
                "legacy-recovery"
                if (record.recovery_command or "").strip()
                else "launch"
            ),
        )

    def _record_with_recovery_recipe(self, record: SessionRecord) -> SessionRecord:
        encoded, digest, policy = self._recipe_values_for_record(record)
        return replace(
            record,
            recovery_recipe=encoded,
            recovery_recipe_sha256=digest,
            restart_policy=policy,
            recovery_attempts=max(0, int(getattr(record, "recovery_attempts", 0))),
        )

    @staticmethod
    def _record_with_authority_context(record: SessionRecord, existing: SessionRecord | None) -> SessionRecord:
        pair = (record.recovery_authority_context, record.recovery_authority_context_sha256)
        if pair == (None, None) and existing is not None:
            # Ordinary tuple/record callers do not make a new observation.
            # Retain existing evidence only for the identical recipe and home.
            if (record.recovery_recipe_sha256 == existing.recovery_recipe_sha256
                    and intent_digest(record) == intent_digest(existing) and context_matches_record(existing)):
                return replace(record, recovery_authority_context=existing.recovery_authority_context,
                               recovery_authority_context_sha256=existing.recovery_authority_context_sha256)
        if context_matches_record(record):
            if pair != (None, None) and (existing is None or pair != (
                    existing.recovery_authority_context, existing.recovery_authority_context_sha256)):
                raise RecoveryAuthorityError("new context requires the observed metadata CAS path")
            return record
        if (existing is not None and pair == (existing.recovery_authority_context, existing.recovery_authority_context_sha256)
                and (record.recovery_recipe_sha256 != existing.recovery_recipe_sha256 or intent_digest(record) != intent_digest(existing))):
            # An intentional task/recipe change invalidates its old binding.
            return replace(record, recovery_authority_context=None, recovery_authority_context_sha256=None)
        raise RecoveryAuthorityError("authority context contradicts session recipe")

    def _recipe_values_for_update(
        self,
        current: SessionRecord,
        updated: SessionRecord,
        changed_fields: set[str],
    ) -> tuple[str | None, str | None, str]:
        """Change intended launch inputs without discarding canonical fields."""

        if current.recovery_recipe is None and current.recovery_recipe_sha256 is None:
            return self._recipe_values_for_record(updated)
        # A malformed existing receipt is evidence to retain, not permission to
        # reconstruct recovery authority from weaker legacy fields.
        recipe = parse_session_recipe(
            current.recovery_recipe, current.recovery_recipe_sha256
        )
        if updated.external:
            return None, None, "never"
        command = recipe.command
        cwd = recipe.cwd
        origin = recipe.origin
        if changed_fields.intersection({"command", "recovery_command"}):
            recovery_command = (updated.recovery_command or "").strip()
            command = recovery_command or updated.command
            origin = "legacy-recovery" if recovery_command else "launch"
        if changed_fields.intersection({"cwd", "recovery_cwd"}):
            cwd = (updated.recovery_cwd or "").strip() or updated.cwd
        return record_recipe_fields(
            command=command,
            cwd=cwd,
            environment=recipe.environment_dict(),
            restart_policy=(
                updated.restart_policy
                if "restart_policy" in changed_fields
                else recipe.restart_policy
            ),
            origin=origin,
        )

    @staticmethod
    def _record_with_recovery_intent(
        record: SessionRecord,
        *,
        existing: SessionRecord | None = None,
        preserve_legacy: bool = False,
    ) -> SessionRecord:
        raw = normalise_recovery_intent(getattr(record, "recovery_intent", None))
        if raw == RECOVERY_INTENT_LEGACY and not preserve_legacy:
            if existing is not None:
                existing_intent = normalise_recovery_intent(existing.recovery_intent)
                if existing_intent in {
                    RECOVERY_INTENT_ACTIVE,
                    RECOVERY_INTENT_INACTIVE,
                }:
                    raw = existing_intent
                else:
                    raw = recovery_intent_for_status(record.status)
            else:
                raw = recovery_intent_for_status(record.status)
        if bool(getattr(record, "external", False)) and raw == RECOVERY_INTENT_LEGACY:
            raw = RECOVERY_INTENT_INACTIVE
        return replace(record, recovery_intent=raw)

    def _backfill_recovery_recipes(self) -> int:
        rows = self.conn.execute("SELECT * FROM sessions").fetchall()
        updates: list[tuple[str | None, str | None, str, str]] = []
        for row in rows:
            record = self._row_to_record(row)
            try:
                encoded, digest, policy = self._recipe_values_for_record(record)
            except SessionRecipeError:
                # Preserve a malformed pre-existing receipt for diagnostics; do
                # not silently replace its authority with mutable legacy fields.
                if row["recovery_recipe"] is not None or row["recovery_recipe_sha256"] is not None:
                    continue
                encoded = digest = None
                policy = "never"
            if (
                row["recovery_recipe"],
                row["recovery_recipe_sha256"],
                str(row["restart_policy"] or DEFAULT_RESTART_POLICY),
            ) != (encoded, digest, policy):
                updates.append((encoded, digest, policy, str(row["id"])))
        if not updates:
            return 0
        self.conn.executemany(
            """
            UPDATE sessions
            SET recovery_recipe = ?, recovery_recipe_sha256 = ?, restart_policy = ?
            WHERE id = ?
            """,
            updates,
        )
        return len(updates)

    def ensure_recovery_recipe(self, session_id: str) -> SessionRecord:
        current = self.get(session_id)
        if current is None:
            raise KeyError(session_id)
        prepared = self._record_with_recovery_recipe(current)
        if prepared == current:
            return current
        self.upsert(prepared)
        refreshed = self.get(session_id)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

    def create(
        self,
        name: str,
        tag: str,
        command: str,
        cwd: str | None = None,
        *,
        window_id: str = "main",
        window_title: str = "Main",
        tab_index: int = 0,
        visible: bool = True,
        pinned: bool = False,
        automatic_name: bool = False,
        environment: Mapping[str, str] | None = None,
        restart_policy: str = DEFAULT_RESTART_POLICY,
    ) -> SessionRecord:
        sid = short_id("td")
        if self.provision_auth_scopes:
            auth_scope_id, codex_home, inherited_account = self._new_auth_scope()
        else:
            auth_scope_id = codex_home = inherited_account = None
        cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
        now = utc_now()
        safe_tag = tmux_safe_name(tag or "default")
        safe_name = tmux_safe_name(name or command or "shell")
        tmux_name = tmux_safe_name(f"termdeck_{safe_tag}_{safe_name}_{sid.replace('-', '_')}")
        recipe_json, recipe_sha256, clean_restart_policy = record_recipe_fields(
            command=command or os.environ.get("SHELL", "/bin/sh"),
            cwd=cwd,
            environment=environment,
            restart_policy=restart_policy,
            origin="launch",
        )
        record = SessionRecord(
            id=sid,
            name=name or command or "shell",
            tag=tag or "default",
            command=command or os.environ.get("SHELL", "/bin/sh"),
            cwd=cwd,
            tmux_name=tmux_name,
            created_at=now,
            last_opened_at=now,
            status="new",
            last_pid=None,
            window_id=window_id or "main",
            window_title=window_title or "Main",
            tab_index=max(0, int(tab_index)),
            visible=bool(visible),
            pinned=bool(pinned),
            pane_target=None,
            external=False,
            recovery_command=None,
            recovery_cwd=None,
            auth_scope_id=auth_scope_id,
            codex_home=codex_home,
            selected_account=inherited_account,
            pending_account=None,
            auth_restart_required=False,
            auth_changed_at=None,
            recovery_recipe=recipe_json,
            recovery_recipe_sha256=recipe_sha256,
            restart_policy=clean_restart_policy,
            recovery_attempts=0,
            recovery_last_attempt_at=None,
            recovery_last_error=None,
            recovery_intent=RECOVERY_INTENT_INACTIVE,
            automatic_name=bool(automatic_name),
        )
        self.upsert(record)
        return record

    def upsert(self, record: SessionRecord, *, preserve_legacy_intent: bool = False) -> None:
        existing = self.get(record.id)
        if existing is not None and record.automatic_name is None:
            # A legacy snapshot cannot erase naming intent recorded by a
            # newer writer or change the old inference by updating cwd.
            # Explicit renames use update with a boolean.
            record = replace(record, automatic_name=existing.name_is_automatic)
        record = self._record_with_recovery_intent(
            record,
            existing=existing,
            preserve_legacy=preserve_legacy_intent,
        )
        if existing is not None and existing.auth_scope_id and existing.codex_home:
            record = replace(
                record,
                auth_scope_id=existing.auth_scope_id,
                codex_home=existing.codex_home,
            )
        elif record.auth_scope_id and record.codex_home:
            from .codex_auth import prepare_auth_scope

            prepare_auth_scope(
                record.codex_home,
                account_source=self._account_source_override,
            )
        elif self.provision_auth_scopes:
            auth_scope_id, codex_home, inherited_account = self._new_auth_scope()
            record = replace(
                record,
                auth_scope_id=auth_scope_id,
                codex_home=codex_home,
                selected_account=inherited_account,
                auth_restart_required=False,
            )
        else:
            record = replace(record, auth_scope_id=None, codex_home=None)
        record = self._record_with_recovery_recipe(record)
        record = self._record_with_authority_context(record, existing)
        if not bool(getattr(record, "external", False)) and record.pane_target is not None:
            record = replace(record, pane_target=None)
        payload = dict(record.__dict__)
        payload["visible"] = 1 if record.visible else 0
        payload["pinned"] = 1 if getattr(record, "pinned", False) else 0
        payload["external"] = 1 if getattr(record, "external", False) else 0
        payload["auth_restart_required"] = 1 if getattr(record, "auth_restart_required", False) else 0
        payload["recovery_attempts"] = max(0, int(getattr(record, "recovery_attempts", 0)))
        payload["automatic_name"] = None if record.automatic_name is None else int(record.automatic_name)
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, name, tag, command, cwd, tmux_name, created_at, last_opened_at,
                status, last_pid, window_id, window_title, tab_index, visible, pane_target, external, pinned,
                recovery_command, recovery_cwd, auth_scope_id, codex_home, selected_account,
                pending_account, auth_restart_required, auth_changed_at,
                recovery_recipe, recovery_recipe_sha256, restart_policy, recovery_attempts,
                recovery_last_attempt_at, recovery_last_error, recovery_intent, automatic_name,
                recovery_authority_context, recovery_authority_context_sha256
            )
            VALUES (
                :id, :name, :tag, :command, :cwd, :tmux_name, :created_at, :last_opened_at,
                :status, :last_pid, :window_id, :window_title, :tab_index, :visible, :pane_target, :external, :pinned,
                :recovery_command, :recovery_cwd, :auth_scope_id, :codex_home, :selected_account,
                :pending_account, :auth_restart_required, :auth_changed_at,
                :recovery_recipe, :recovery_recipe_sha256, :restart_policy, :recovery_attempts,
                :recovery_last_attempt_at, :recovery_last_error, :recovery_intent, :automatic_name,
                :recovery_authority_context, :recovery_authority_context_sha256
            )
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                tag = excluded.tag,
                command = excluded.command,
                cwd = excluded.cwd,
                tmux_name = excluded.tmux_name,
                last_opened_at = excluded.last_opened_at,
                status = excluded.status,
                last_pid = excluded.last_pid,
                window_id = excluded.window_id,
                window_title = excluded.window_title,
                tab_index = excluded.tab_index,
                visible = excluded.visible,
                pinned = excluded.pinned,
                pane_target = excluded.pane_target,
                external = excluded.external,
                recovery_command = excluded.recovery_command,
                recovery_cwd = excluded.recovery_cwd,
                selected_account = excluded.selected_account,
                pending_account = excluded.pending_account,
                auth_restart_required = excluded.auth_restart_required,
                auth_changed_at = excluded.auth_changed_at,
                recovery_recipe = excluded.recovery_recipe,
                recovery_recipe_sha256 = excluded.recovery_recipe_sha256,
                restart_policy = excluded.restart_policy,
                recovery_attempts = excluded.recovery_attempts,
                recovery_last_attempt_at = excluded.recovery_last_attempt_at,
                recovery_last_error = excluded.recovery_last_error,
                recovery_intent = excluded.recovery_intent,
                automatic_name = excluded.automatic_name,
                recovery_authority_context = excluded.recovery_authority_context,
                recovery_authority_context_sha256 = excluded.recovery_authority_context_sha256
            """,
            payload,
        )
        # A tab moved to another window cannot remain captain of its previous
        # window. Keep this invariant in the store instead of relying on every
        # lifecycle or UI caller to remember the cleanup.
        self.conn.execute(
            "DELETE FROM window_captains WHERE session_id = ? AND window_id != ?",
            (record.id, record.window_id),
        )
        if record.visible and record.status.casefold() != "archived":
            now = utc_now()
            self.conn.execute(
                """
                INSERT INTO remembered_windows (
                    window_id, window_title, created_at, last_opened_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(window_id) DO UPDATE SET
                    window_title = excluded.window_title,
                    last_opened_at = excluded.last_opened_at
                """,
                (record.window_id, record.window_title or "Window", now, now),
            )
        self.conn.commit()

    def update(self, session_id: str, **changes: object) -> SessionRecord:
        current = self.get(session_id)
        if not current:
            raise KeyError(session_id)
        for field in ("auth_scope_id", "codex_home"):
            if field in changes and changes[field] != getattr(current, field):
                raise ValueError(f"{field} is immutable for terminal {session_id}")
        data: dict[str, Any] = dict(current.__dict__)
        data.update(changes)
        if "automatic_name" not in changes:
            # Name writes express a manual rename unless a directory observer
            # explicitly retains automatic mode. Other changes freeze legacy
            # inference before a cwd update can alter its meaning.
            data["automatic_name"] = False if "name" in changes else current.name_is_automatic
        if "recovery_intent" in changes:
            data["recovery_intent"] = normalise_recovery_intent(
                changes["recovery_intent"]
            )
        elif "status" in changes:
            status = str(data.get("status", "")).strip().casefold()
            if status == "start-error":
                # Preserve an already-proven active task across a failed
                # restart; an initial launch remains ambiguous and fail-closed.
                data["recovery_intent"] = (
                    RECOVERY_INTENT_ACTIVE
                    if current.recovery_intent == RECOVERY_INTENT_ACTIVE
                    else RECOVERY_INTENT_LEGACY
                )
            else:
                data["recovery_intent"] = recovery_intent_for_status(status)
        recipe_inputs = {
            "command",
            "cwd",
            "recovery_command",
            "recovery_cwd",
            "restart_policy",
        }
        changed_recipe_inputs = {
            field
            for field in recipe_inputs.intersection(changes)
            if changes[field] != getattr(current, field)
        }
        if changed_recipe_inputs and not {
            "recovery_recipe",
            "recovery_recipe_sha256",
        }.intersection(changes):
            encoded, digest, policy = self._recipe_values_for_update(
                current, SessionRecord(**data), changed_recipe_inputs
            )
            data["recovery_recipe"] = encoded
            data["recovery_recipe_sha256"] = digest
            data["restart_policy"] = policy
        if "last_opened_at" not in changes:
            data["last_opened_at"] = utc_now()
        if "visible" in data:
            data["visible"] = bool(data["visible"])
        if "tab_index" in data:
            data["tab_index"] = max(0, int(data["tab_index"]))
        if "pinned" in data:
            data["pinned"] = bool(data["pinned"])
        if "external" in data:
            data["external"] = bool(data["external"])
        if not bool(data.get("external", False)):
            data["pane_target"] = None
        if "auth_restart_required" in data:
            data["auth_restart_required"] = bool(data["auth_restart_required"])
        if "recovery_attempts" in data:
            data["recovery_attempts"] = max(0, int(data["recovery_attempts"]))
        preserve_legacy_intent = (
            "recovery_intent" in changes
            or str(data.get("status", "")).strip().casefold() == "start-error"
        )
        updated = self._record_with_recovery_recipe(SessionRecord(**data))
        updated = self._record_with_recovery_intent(
            updated,
            existing=current,
            preserve_legacy=preserve_legacy_intent,
        )
        self.upsert(
            updated,
            preserve_legacy_intent=preserve_legacy_intent,
        )
        return self.get(session_id) or updated

    def set_recovery_intent(self, session_id: str, intent: object) -> SessionRecord:
        """Persist an explicit lifecycle intent for one session."""

        normalized = normalise_recovery_intent(intent)
        if normalized not in RECOVERY_INTENTS:
            raise ValueError(f"unsupported recovery intent: {intent!r}")
        return self.update(
            session_id,
            recovery_intent=normalized,
        )

    def mark_recovery_active(self, session_id: str) -> SessionRecord:
        return self.set_recovery_intent(session_id, RECOVERY_INTENT_ACTIVE)

    def mark_recovery_inactive(self, session_id: str) -> SessionRecord:
        return self.set_recovery_intent(session_id, RECOVERY_INTENT_INACTIVE)

    @staticmethod
    def session_row_sha256(row: Mapping[str, object]) -> str:
        """Bind every stored field, including legacy NULL values, for repair CAS."""
        encoded = json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def reenroll_recovery_receipts(
        self,
        expected_row_sha256: Mapping[str, str],
        *,
        assert_quiescent: Callable[[list[SessionRecord]], None],
    ) -> dict[str, str]:
        """Explicitly enroll unchanged canonical payloads under full-row CAS.

        The operator must hold the shared recovery target writer locks and
        provide an authoritative quiescence assertion, executed while the
        SQLite write lock is held. This operation preserves every other raw
        session field and all remembered-window/lifecycle tables. It neither
        starts terminals nor provisions or changes authentication.
        """
        expected = dict(expected_row_sha256)
        if not expected or len(expected) > 1000 or not callable(assert_quiescent):
            raise ValueError("receipt reenrollment selection is invalid")
        if any(not isinstance(key, str) or not key or
               not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for key, value in expected.items()):
            raise ValueError("receipt reenrollment preimages are invalid")
        if self.conn.in_transaction:
            raise ValueError("receipt reenrollment requires its own transaction")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            records: list[SessionRecord] = []
            updates: list[tuple[str, str, str]] = []
            for session_id in sorted(expected):
                row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row is None or self.session_row_sha256(row) != expected[session_id]:
                    raise ValueError("receipt reenrollment preimage changed")
                record = self._row_to_record(row)
                encoded, digest = receipt_reenrollment(record)
                records.append(record)
                updates.append((encoded, digest, session_id))
            assert_quiescent(records)
            self.conn.executemany(
                "UPDATE sessions SET recovery_recipe = ?, recovery_recipe_sha256 = ? WHERE id = ?",
                updates,
            )
            self.conn.commit()
            return {session_id: digest for _encoded, digest, session_id in updates}
        except BaseException:
            self.conn.rollback()
            raise

    def rollback_receipt_reenrollment(
        self,
        preimages: Mapping[str, Mapping[str, object]],
        *,
        assert_quiescent: Callable[[list[SessionRecord]], None],
    ) -> set[str]:
        """Restore quarantined receipts only while every enrolled row is unchanged.

        This explicit inverse has the same writer/quiescence requirements as
        reenrollment. It deliberately restores the invalid original receipt;
        normal startup will reject it again. No later lifecycle or user edits
        can be overwritten by this rollback.
        """
        if (not preimages or len(preimages) > 1000 or not callable(assert_quiescent)
                or self.conn.in_transaction):
            raise ValueError("receipt rollback selection or transaction is invalid")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            records = []
            updates = []
            for session_id, preimage in sorted(preimages.items()):
                row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row is None or preimage.get("id") != session_id:
                    raise ValueError("receipt rollback preimage is invalid")
                original = self._row_to_record(preimage)
                encoded, digest = receipt_reenrollment(original)
                expected = dict(preimage, recovery_recipe=encoded, recovery_recipe_sha256=digest)
                if dict(row) != expected:
                    raise ValueError("receipt rollback enrolled row changed")
                records.append(self._row_to_record(row))
                updates.append((preimage["recovery_recipe"], preimage["recovery_recipe_sha256"], session_id))
            assert_quiescent(records)
            self.conn.executemany(
                "UPDATE sessions SET recovery_recipe = ?, recovery_recipe_sha256 = ? WHERE id = ?",
                updates,
            )
            self.conn.commit()
            return set(preimages)
        except BaseException:
            self.conn.rollback()
            raise

    def reconcile_runtime_states(
        self,
        updates: Iterable[tuple[str, str, bool, int | None]],
        expected: dict[str, SessionRecord],
    ) -> set[str]:
        """Apply only observations whose complete durable record is unchanged.

        Hold the write lock across comparison and mutation so concurrent stop,
        recovery, moves and account changes cannot be overwritten by old IO.
        """
        rows = list(updates)
        if not rows:
            return set()
        try:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            accepted = [row for row in rows if self.get(row[0]) == expected.get(row[0])]
            self.update_runtime_states(accepted)
            self.conn.commit()
            return {row[0] for row in accepted}
        except BaseException:
            self.conn.rollback()
            raise

    def update_runtime_states(
        self,
        updates: Iterable[tuple[str, str, bool, int | None]],
    ) -> int:
        """Apply reconciliation state changes without reordering browser history."""

        rows = list(updates)
        if not rows:
            return 0
        changed = 0
        active_statuses = _ACTIVE_RECOVERY_STATUSES | {"start-error"}
        for session_id, status, visible, last_pid in rows:
            if str(status).strip().casefold() in active_statuses:
                cursor = self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, visible = ?, last_pid = ?,
                        recovery_intent = CASE
                            WHEN recovery_intent = ? THEN recovery_intent
                            ELSE ?
                        END
                    WHERE id = ?
                      AND NOT (
                          recovery_intent = ?
                          AND status IN ('stopped', 'archived')
                      )
                    """,
                    (
                        status,
                        1 if visible else 0,
                        last_pid,
                        RECOVERY_INTENT_INACTIVE,
                        RECOVERY_INTENT_ACTIVE,
                        session_id,
                        RECOVERY_INTENT_INACTIVE,
                    ),
                )
            else:
                # A missing/stopped observation is not an explicit user stop.
                # Never clear active intent here; the next startup can still
                # recover the task from its durable recipe.
                cursor = self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, visible = ?, last_pid = ?
                    WHERE id = ?
                    """,
                    (status, 1 if visible else 0, last_pid, session_id),
                )
            changed += max(0, int(cursor.rowcount))
        self.conn.commit()
        return changed

    def update_recovery_commands(
        self,
        updates: Iterable[tuple[str, str | None]],
    ) -> int:
        """Compatibility helper for callers without recovery cwd metadata."""

        return self.update_recovery_metadata(
            (session_id, command, None) for session_id, command in updates
        )

    def update_recovery_metadata(
        self,
        updates: Iterable[tuple[str, str | None, str | None]],
        *,
        authority_observations: Mapping[str, AuthorityObservation] | None = None,
    ) -> int:
        """Persist changed crash-relaunch commands and cwd values atomically."""

        requested = [
            (session_id, command, cwd if command else None)
            for session_id, command, cwd in updates
        ]
        if not requested:
            return 0
        try:
            # Read and write the recipe under one lock: a concurrent observer
            # must not replace a canonical receipt between validation and use.
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            identifiers = [session_id for session_id, _command, _cwd in requested]
            placeholders = ",".join("?" for _ in identifiers)
            existing = {
                str(row["id"]): self._row_to_record(row)
                for row in self.conn.execute(
                    f"SELECT * FROM sessions WHERE id IN ({placeholders}) AND external = 0",
                    identifiers,
                ).fetchall()
            }
            rows = []
            for session_id, command, cwd in requested:
                current = existing.get(session_id)
                if current is None:
                    continue
                observation = (authority_observations or {}).get(session_id)
                if observation is not None and observation.expected_record_sha256 != record_cas(current):
                    # An asynchronous observation may not overwrite a newer
                    # intent, receipt, account selection or runtime generation.
                    continue
                if observation is not None and observation.is_current is not None and not observation.is_current():
                    continue
                if (
                    command is not None
                    and recovery_command_specificity(current.recovery_command)
                    > recovery_command_specificity(command)
                ):
                    # Preserve the more-specific opaque resume target. A
                    # positive None observation still clears ended metadata.
                    if observation is None:
                        continue
                    command, cwd = current.recovery_command, current.recovery_cwd
                changed_fields = {
                    field
                    for field, value in (
                        ("recovery_command", command), ("recovery_cwd", cwd)
                    )
                    if value != getattr(current, field)
                }
                if not changed_fields and observation is None:
                    continue
                updated = replace(current, recovery_command=command, recovery_cwd=cwd)
                if changed_fields:
                    encoded, digest, policy = self._recipe_values_for_update(current, updated, changed_fields)
                else:
                    encoded, digest, policy = self._recipe_values_for_record(current)
                context = current.recovery_authority_context
                context_digest = current.recovery_authority_context_sha256
                if observation is not None:
                    if observation.encoded is None and observation.sha256 is None:
                        # Lost/weak or reused-owner observations cannot silently
                        # promote a prior generation's context to the new one.
                        context = context_digest = None
                    else:
                        value = parse_context(observation.encoded, observation.sha256)
                        if value["native_producer"] is not None and observation.is_current is None:
                            raise RecoveryAuthorityError("native enrollment requires current observed owner proof")
                        context, context_digest = bind_context(observation.encoded, observation.sha256,
                                                              updated, encoded, digest)
                        if current.recovery_authority_context is not None:
                            context, context_digest = retain_stronger_context(
                                (current.recovery_authority_context, current.recovery_authority_context_sha256),
                                (context, context_digest))
                elif changed_fields:
                    context = context_digest = None
                if not changed_fields and (context, context_digest) == (current.recovery_authority_context, current.recovery_authority_context_sha256):
                    continue
                rows.append(
                    (
                        command, cwd, encoded, digest, policy, context, context_digest, session_id,
                    )
                )
                existing[session_id] = replace(
                    updated,
                    recovery_recipe=encoded,
                    recovery_recipe_sha256=digest,
                    restart_policy=policy,
                    recovery_authority_context=context,
                    recovery_authority_context_sha256=context_digest,
                )
            # Validate every changed recipe before the first write, then commit
            # legacy metadata and canonical authority in the same statement.
            cursor = self.conn.executemany(
                """
                UPDATE sessions
                SET recovery_command = ?, recovery_cwd = ?,
                    recovery_recipe = ?, recovery_recipe_sha256 = ?, restart_policy = ?,
                    recovery_authority_context = ?, recovery_authority_context_sha256 = ?
                WHERE id = ? AND external = 0
                """,
                rows,
            )
            self.conn.commit()
            return max(0, cursor.rowcount)
        except BaseException:
            self.conn.rollback()
            raise

    def get(self, session_id: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_tmux_name(self, tmux_name: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE tmux_name = ?", (tmux_name,)).fetchone()
        return self._row_to_record(row) if row else None

    def set_window_captain(self, window_id: str, session_id: str) -> SessionRecord:
        """Designate one session as the captain of its current window."""

        record = self.get(session_id)
        if record is None:
            raise KeyError(session_id)
        if record.window_id != window_id:
            raise ValueError(
                f"session {session_id} belongs to window {record.window_id!r}, not {window_id!r}"
            )
        # The UNIQUE session_id constraint prevents one tab from captaining two
        # windows. Delete any stale prior assignment before the window upsert.
        self.conn.execute("DELETE FROM window_captains WHERE session_id = ?", (session_id,))
        self.conn.execute(
            """
            INSERT INTO window_captains (window_id, session_id)
            VALUES (?, ?)
            ON CONFLICT(window_id) DO UPDATE SET session_id = excluded.session_id
            """,
            (window_id, session_id),
        )
        self.conn.commit()
        return record

    def captain_for_window(self, window_id: str) -> SessionRecord | None:
        """Return a window's captain, removing a missing or moved assignment."""

        row = self.conn.execute(
            "SELECT session_id FROM window_captains WHERE window_id = ?",
            (window_id,),
        ).fetchone()
        if row is None:
            return None
        session_id = str(row["session_id"])
        record = self.get(session_id)
        if record is None or record.window_id != window_id:
            self.clear_window_captain(window_id, session_id=session_id)
            return None
        return record

    def clear_window_captain(self, window_id: str, session_id: str | None = None) -> bool:
        """Clear a captain, optionally only when it still matches ``session_id``."""

        if session_id is None:
            cursor = self.conn.execute(
                "DELETE FROM window_captains WHERE window_id = ?",
                (window_id,),
            )
        else:
            cursor = self.conn.execute(
                "DELETE FROM window_captains WHERE window_id = ? AND session_id = ?",
                (window_id, session_id),
            )
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def _cleanup_stale_window_captains(self) -> int:
        cursor = self.conn.execute(
            """
            DELETE FROM window_captains
            WHERE NOT EXISTS (
                SELECT 1
                FROM sessions
                WHERE sessions.id = window_captains.session_id
                  AND sessions.window_id = window_captains.window_id
            )
            """
        )
        return max(0, int(cursor.rowcount))

    def list(
        self,
        include_archived: bool = False,
        *,
        visible_only: bool = False,
        window_id: str | None = None,
        include_stopped: bool = True,
    ) -> list[SessionRecord]:
        where: list[str] = []
        params: list[object] = []
        if not include_archived:
            where.append("status != 'archived'")
        if visible_only:
            where.append("visible = 1")
        if not include_stopped:
            where.append("status != 'stopped'")
        if window_id is not None:
            where.append("window_id = ?")
            params.append(window_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.conn.execute(
            f"SELECT * FROM sessions {clause} ORDER BY window_title, window_id, pinned DESC, tab_index, last_opened_at DESC",
            params,
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def visible_records(self) -> list[SessionRecord]:
        return self.list(visible_only=True, include_stopped=False)

    def hidden_records(
        self,
        *,
        limit: int = 20,
        tag: str | None = None,
        include_archived: bool = False,
        include_stopped: bool = False,
    ) -> list[SessionRecord]:
        """Return recently hidden browser tabs without exposing process plumbing."""

        clean_limit = max(1, int(limit))
        where = ["visible = 0"]
        params: list[object] = []
        if not include_archived:
            where.append("status != 'archived'")
        if not include_stopped:
            where.append("status != 'stopped'")
        if tag:
            where.append("tag = ?")
            params.append(tag)
        clause = " AND ".join(where)
        rows = self.conn.execute(
            f"""
            SELECT * FROM sessions
            WHERE {clause}
            ORDER BY last_opened_at DESC, created_at DESC, name ASC
            LIMIT ?
            """,
            (*params, clean_limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def restore_candidates(
        self,
        limit: int | None = None,
        *,
        window_id: str | None = None,
    ) -> list[SessionRecord]:
        """Return every visible restorable tab, optionally bounded by callers.

        Browser restoration must never silently discard tab 13 or later. The
        unbounded default is therefore authoritative for GUI/startup recovery;
        bounded callers may still request a positive prefix for previews or
        diagnostics. When ``window_id`` is set, rows remain scoped to that
        logical window so restore cannot steal tabs from another window.
        """

        rows = self.list(visible_only=True, window_id=window_id, include_stopped=False)
        if limit is None:
            return rows
        clean_limit = max(1, int(limit))
        return rows[:clean_limit]

    def rename_window(self, window_id: str, title: str) -> int:
        """Rename one logical TermDeck window without changing tab timestamps."""

        clean_title = title.strip() or "Window"
        cursor = self.conn.execute(
            "UPDATE sessions SET window_title = ? WHERE window_id = ?",
            (clean_title, window_id),
        )
        self.conn.execute(
            """
            UPDATE remembered_windows
            SET window_title = ?, last_opened_at = ?
            WHERE window_id = ?
            """,
            (clean_title, utc_now(), window_id),
        )
        self.conn.commit()
        return max(0, int(cursor.rowcount))

    def windows_with_visible_sessions(self) -> list[tuple[str, str, list[SessionRecord]]]:
        records = self.visible_records()
        grouped: dict[str, list[SessionRecord]] = {}
        titles: dict[str, str] = {}
        for record in records:
            grouped.setdefault(record.window_id, []).append(record)
            titles.setdefault(record.window_id, record.window_title)
        result: list[tuple[str, str, list[SessionRecord]]] = []
        for window_id in sorted(grouped, key=lambda wid: (titles.get(wid, ""), wid)):
            rows = sorted(grouped[window_id], key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at))
            result.append((window_id, titles.get(window_id, "Window"), rows))
        return result

    def windows_for_restore(self) -> list[tuple[str, str, list[SessionRecord]]]:
        """Return every durable window, including windows with no live tabs."""

        records = self.visible_records()
        grouped: dict[str, list[SessionRecord]] = {}
        titles: dict[str, str] = {}
        for record in records:
            grouped.setdefault(record.window_id, []).append(record)
            titles.setdefault(record.window_id, record.window_title)

        remembered = self.remembered_windows()
        known_ids = {window_id for window_id, _title in remembered}
        # Defensive compatibility for a database written by another process
        # between schema initialization and this read.
        remembered.extend(
            (window_id, titles.get(window_id, "Window"))
            for window_id in grouped
            if window_id not in known_ids
        )
        remembered.sort(key=lambda item: (item[1].casefold(), item[0]))

        result: list[tuple[str, str, list[SessionRecord]]] = []
        for window_id, window_title in remembered:
            rows = sorted(
                grouped.get(window_id, []),
                key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at),
            )
            result.append((window_id, window_title, rows))
        return result

    def find_equivalent(
        self,
        *,
        name: str,
        tag: str,
        command: str,
        cwd: str,
        window_title: str | None = None,
    ) -> SessionRecord | None:
        """Find a remembered session matching a workspace recipe tab."""

        rows = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE name = ? AND tag = ? AND command = ? AND cwd = ?
              AND status != 'archived'
            ORDER BY CASE WHEN window_title = ? THEN 0 ELSE 1 END, pinned DESC, last_opened_at DESC
            LIMIT 1
            """,
            (name, tag, command, str(Path(cwd).expanduser()), window_title or ""),
        ).fetchall()
        return self._row_to_record(rows[0]) if rows else None

    def tags(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT tag FROM sessions WHERE status != 'archived' ORDER BY tag").fetchall()
        return [str(row[0]) for row in rows]

    def delete(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM window_captains WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            name=row["name"],
            automatic_name=None if row["automatic_name"] is None else bool(row["automatic_name"]),
            tag=row["tag"],
            command=row["command"],
            cwd=row["cwd"],
            tmux_name=row["tmux_name"],
            created_at=row["created_at"],
            last_opened_at=row["last_opened_at"],
            status=row["status"],
            last_pid=row["last_pid"],
            window_id=row["window_id"],
            window_title=row["window_title"],
            tab_index=int(row["tab_index"]),
            visible=bool(row["visible"]),
            pinned=bool(row["pinned"]),
            pane_target=row["pane_target"] if bool(row["external"]) else None,
            external=bool(row["external"]),
            recovery_command=row["recovery_command"],
            recovery_cwd=row["recovery_cwd"],
            auth_scope_id=row["auth_scope_id"],
            codex_home=row["codex_home"],
            selected_account=row["selected_account"],
            pending_account=row["pending_account"],
            auth_restart_required=bool(row["auth_restart_required"]),
            auth_changed_at=row["auth_changed_at"],
            recovery_recipe=row["recovery_recipe"],
            recovery_recipe_sha256=row["recovery_recipe_sha256"],
            restart_policy=str(row["restart_policy"] or DEFAULT_RESTART_POLICY),
            recovery_attempts=max(0, int(row["recovery_attempts"] or 0)),
            recovery_last_attempt_at=row["recovery_last_attempt_at"],
            recovery_last_error=row["recovery_last_error"],
            recovery_intent=normalise_recovery_intent(row["recovery_intent"]),
            recovery_authority_context=row["recovery_authority_context"] if "recovery_authority_context" in row.keys() else None,
            recovery_authority_context_sha256=row["recovery_authority_context_sha256"] if "recovery_authority_context_sha256" in row.keys() else None,
        )
