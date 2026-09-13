"""Bounded, non-mutating SQLite read-only refresh support.

TermDeck keeps durable session state in SQLite with WAL enabled. A reader can
observe a short replacement window while a database or one of its WAL/SHM
sidecars is being rotated. This module makes that boundary explicit: only
read-only connections are opened, retries are bounded, and the final error
retains SQLite's extended result code together with the affected paths.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
_SQLITE_PRIMARY_MASK = 0xFF
_DEFAULT_ATTEMPTS = 3
_DEFAULT_DELAY_SECONDS = 0.025


def _absolute_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate == Path(":memory:"):
        raise ValueError("SQLite read-only refresh requires a persistent database")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


@dataclass(frozen=True)
class SQLiteFileState:
    """Observable identity for one SQLite file without reading its contents."""

    path: str
    exists: bool
    regular: bool
    identity: tuple[int, int] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "regular": self.regular,
            "identity": list(self.identity) if self.identity is not None else None,
        }


@dataclass(frozen=True)
class SQLiteReadOnlyState:
    """Database and sidecar identities sampled around one read-only refresh."""

    database: SQLiteFileState
    wal: SQLiteFileState
    shm: SQLiteFileState
    journal: SQLiteFileState

    @property
    def paths(self) -> tuple[str, ...]:
        return (
            self.database.path,
            self.wal.path,
            self.shm.path,
            self.journal.path,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database.as_dict(),
            "wal": self.wal.as_dict(),
            "shm": self.shm.as_dict(),
            "journal": self.journal.as_dict(),
        }


def _file_state(path: Path) -> SQLiteFileState:
    try:
        metadata = os.lstat(path)
    except OSError:
        return SQLiteFileState(str(path), False, False, None)
    regular = stat.S_ISREG(metadata.st_mode)
    identity = (int(metadata.st_dev), int(metadata.st_ino)) if regular else None
    return SQLiteFileState(str(path), True, regular, identity)


def sqlite_read_only_state(path: str | Path) -> SQLiteReadOnlyState:
    """Return database/WAL/SHM/journal identities without mutating state."""

    database = _absolute_path(path)
    return SQLiteReadOnlyState(
        database=_file_state(database),
        wal=_file_state(Path(f"{database}-wal")),
        shm=_file_state(Path(f"{database}-shm")),
        journal=_file_state(Path(f"{database}-journal")),
    )


def sqlite_read_only_state_changed(
    before: SQLiteReadOnlyState,
    after: SQLiteReadOnlyState,
) -> bool:
    """Report replacement/creation/removal, ignoring normal WAL growth."""

    for before_file, after_file in zip(
        (before.database, before.wal, before.shm, before.journal),
        (after.database, after.wal, after.shm, after.journal),
    ):
        if before_file.exists != after_file.exists:
            return True
        if before_file.regular != after_file.regular:
            return True
        if before_file.identity != after_file.identity:
            return True
    return False


def _sqlite_error_values(
    error: BaseException | None,
) -> tuple[int | None, str | None, int | None, int | None, str | None]:
    raw_code = getattr(error, "sqlite_errorcode", None)
    code = int(raw_code) if isinstance(raw_code, int) else None
    raw_name = getattr(error, "sqlite_errorname", None)
    name = str(raw_name) if raw_name else None
    primary: int | None = None
    subtype: int | None = None
    subtype_name: str | None = None
    if code is not None:
        primary = code & _SQLITE_PRIMARY_MASK
        if primary == getattr(sqlite3, "SQLITE_IOERR", 10):
            subtype = code >> 8
            if name is None:
                for candidate, value in vars(sqlite3).items():
                    if candidate.startswith("SQLITE_IOERR_") and value == code:
                        subtype_name = candidate
                        break
        elif name is None:
            for candidate, value in vars(sqlite3).items():
                if candidate.startswith("SQLITE_") and value == code:
                    name = candidate
                    break
    if name and name.startswith("SQLITE_IOERR_"):
        subtype_name = name
    return code, name, primary, subtype, subtype_name


@dataclass(frozen=True)
class SQLiteReadOnlyFailure:
    """Structured failure information safe to include in a diagnostic."""

    operation: str
    database_path: str
    paths: tuple[str, ...]
    message: str
    attempt: int
    max_attempts: int
    transient: bool
    reason: str
    sqlite_extended_code: int | None = None
    sqlite_error_name: str | None = None
    sqlite_primary_code: int | None = None
    sqlite_ioerr_subtype: int | None = None
    sqlite_ioerr_subtype_name: str | None = None
    state_before: SQLiteReadOnlyState | None = None
    state_after: SQLiteReadOnlyState | None = None
    data_version_before: int | None = None
    data_version_after: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "database_path": self.database_path,
            "paths": list(self.paths),
            "message": self.message,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "transient": self.transient,
            "reason": self.reason,
            "sqlite_extended_code": self.sqlite_extended_code,
            "sqlite_error_name": self.sqlite_error_name,
            "sqlite_primary_code": self.sqlite_primary_code,
            "sqlite_ioerr_subtype": self.sqlite_ioerr_subtype,
            "sqlite_ioerr_subtype_name": self.sqlite_ioerr_subtype_name,
            "state_before": self.state_before.as_dict() if self.state_before else None,
            "state_after": self.state_after.as_dict() if self.state_after else None,
            "data_version_before": self.data_version_before,
            "data_version_after": self.data_version_after,
        }

    def render(self) -> str:
        code = ""
        if self.sqlite_extended_code is not None:
            code = f" sqlite_errorcode={self.sqlite_extended_code}"
            if self.sqlite_error_name:
                code += f" ({self.sqlite_error_name})"
            if self.sqlite_ioerr_subtype is not None:
                code += f" ioerr_subtype={self.sqlite_ioerr_subtype}"
        retry = "transient" if self.transient else "persistent"
        return (
            f"{self.operation} failed ({retry}) at {self.database_path}: "
            f"{self.message}{code}; paths={','.join(self.paths)}; "
            f"attempt={self.attempt}/{self.max_attempts}; reason={self.reason}"
        )


class SQLiteReadOnlyError(RuntimeError):
    """A read-only refresh failed after its bounded retry policy."""

    def __init__(self, failure: SQLiteReadOnlyFailure) -> None:
        self.failure = failure
        super().__init__(failure.render())


@dataclass(frozen=True)
class SQLiteReadOnlyResult(Generic[T]):
    value: T
    attempts: int
    data_version_before: int | None
    data_version_after: int | None
    state_before: SQLiteReadOnlyState
    state_after: SQLiteReadOnlyState


def _data_version(connection: object) -> int | None:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None:
        return None
    return int(row[0])


def _configure_connection(connection: object) -> None:
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 500")


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    """Open a regular file in URI read-only mode and enable query-only mode."""

    requested = _absolute_path(path)
    try:
        metadata = os.lstat(requested)
    except OSError as exc:
        raise OSError(f"SQLite database is unavailable: {requested}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"SQLite database is not a regular file: {requested}")
    source = requested.resolve(strict=True)
    connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=0.5,
    )
    connection.row_factory = sqlite3.Row
    _configure_connection(connection)
    return connection


def _retryable(
    error: BaseException,
    before: SQLiteReadOnlyState,
    after: SQLiteReadOnlyState,
) -> tuple[bool, str]:
    if sqlite_read_only_state_changed(before, after):
        return True, "database or WAL/SHM/journal identity changed during refresh"
    _code, _name, primary, _subtype, _subtype_name = _sqlite_error_values(error)
    if primary in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }:
        return True, "SQLite reported a busy or locked read-only snapshot"
    if primary == getattr(sqlite3, "SQLITE_IOERR", 10):
        return True, "SQLite reported an I/O error while reading a read-only snapshot"
    if isinstance(error, OSError) and error.errno in {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.ESTALE,
    }:
        return True, "filesystem reported a transient read-only refresh error"
    lowered = str(error).casefold()
    if "database is locked" in lowered or "database is busy" in lowered:
        return True, "SQLite reported a busy or locked read-only snapshot"
    return False, "persistent SQLite or filesystem error"


def read_only_refresh(
    path: str | Path,
    reader: Callable[[sqlite3.Connection], T],
    *,
    operation: str = "SQLite read-only refresh",
    opener: Callable[[Path], sqlite3.Connection] | None = None,
    attempts: int = _DEFAULT_ATTEMPTS,
    delay_seconds: float = _DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> SQLiteReadOnlyResult[T]:
    """Run a bounded read-only refresh and reopen after transient failures.

    The callback must only query ``connection``. A path or sidecar identity
    change, SQLite busy/locked result, or SQLite IOERR is retried at most
    ``attempts`` times. Corruption, malformed schema, missing files and other
    persistent failures are surfaced immediately with their exact diagnostics.
    No database, WAL, SHM or journal file is created, deleted, or rewritten.
    """

    if attempts < 1:
        raise ValueError("read-only refresh attempts must be at least one")
    if delay_seconds < 0:
        raise ValueError("read-only refresh delay cannot be negative")
    requested = _absolute_path(path)
    connect = opener or connect_read_only
    last_failure: SQLiteReadOnlyFailure | None = None
    for attempt in range(1, attempts + 1):
        before_state = sqlite_read_only_state(requested)
        connection: sqlite3.Connection | None = None
        result: T | None = None
        data_before: int | None = None
        data_after: int | None = None
        failure: SQLiteReadOnlyFailure | None = None
        try:
            connection = connect(requested)
            _configure_connection(connection)
            data_before = _data_version(connection)
            result = reader(connection)
            data_after = _data_version(connection)
            after_state = sqlite_read_only_state(requested)
            state_changed = sqlite_read_only_state_changed(before_state, after_state)
            version_changed = (
                data_before is not None
                and data_after is not None
                and data_before != data_after
            )
            if state_changed or version_changed:
                reason = (
                    "database or WAL/SHM/journal identity changed during refresh"
                    if state_changed
                    else "SQLite data_version changed during refresh"
                )
                failure = SQLiteReadOnlyFailure(
                    operation=operation,
                    database_path=str(requested),
                    paths=after_state.paths,
                    message="read-only snapshot was not stable",
                    attempt=attempt,
                    max_attempts=attempts,
                    transient=True,
                    reason=reason,
                    state_before=before_state,
                    state_after=after_state,
                    data_version_before=data_before,
                    data_version_after=data_after,
                )
            else:
                return SQLiteReadOnlyResult(
                    value=result,
                    attempts=attempt,
                    data_version_before=data_before,
                    data_version_after=data_after,
                    state_before=before_state,
                    state_after=after_state,
                )
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            after_state = sqlite_read_only_state(requested)
            transient, reason = _retryable(exc, before_state, after_state)
            code, name, primary, subtype, subtype_name = _sqlite_error_values(exc)
            failure = SQLiteReadOnlyFailure(
                operation=operation,
                database_path=str(requested),
                paths=after_state.paths,
                message=str(exc),
                attempt=attempt,
                max_attempts=attempts,
                transient=transient,
                reason=reason,
                sqlite_extended_code=code,
                sqlite_error_name=name,
                sqlite_primary_code=primary,
                sqlite_ioerr_subtype=subtype,
                sqlite_ioerr_subtype_name=subtype_name,
                state_before=before_state,
                state_after=after_state,
                data_version_before=data_before,
                data_version_after=data_after,
            )
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, sqlite3.Error) as exc:
                    if failure is None:
                        after_state = sqlite_read_only_state(requested)
                        transient, reason = _retryable(exc, before_state, after_state)
                        code, name, primary, subtype, subtype_name = _sqlite_error_values(exc)
                        failure = SQLiteReadOnlyFailure(
                            operation=operation,
                            database_path=str(requested),
                            paths=after_state.paths,
                            message=str(exc),
                            attempt=attempt,
                            max_attempts=attempts,
                            transient=transient,
                            reason=reason,
                            sqlite_extended_code=code,
                            sqlite_error_name=name,
                            sqlite_primary_code=primary,
                            sqlite_ioerr_subtype=subtype,
                            sqlite_ioerr_subtype_name=subtype_name,
                            state_before=before_state,
                            state_after=after_state,
                            data_version_before=data_before,
                            data_version_after=data_after,
                        )
        if failure is None:
            raise RuntimeError("read-only refresh ended without a result or failure")
        last_failure = failure
        if not failure.transient or attempt >= attempts:
            raise SQLiteReadOnlyError(failure)
        sleep(delay_seconds * attempt)
    assert last_failure is not None
    raise SQLiteReadOnlyError(last_failure)


__all__ = [
    "SQLiteFileState",
    "SQLiteReadOnlyError",
    "SQLiteReadOnlyFailure",
    "SQLiteReadOnlyResult",
    "SQLiteReadOnlyState",
    "connect_read_only",
    "read_only_refresh",
    "sqlite_read_only_state",
    "sqlite_read_only_state_changed",
]
