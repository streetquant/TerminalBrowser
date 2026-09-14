from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from termdeck.sqlite_io import (
    SQLiteReadOnlyError,
    read_only_refresh,
    sqlite_read_only_state,
    sqlite_read_only_state_changed,
)


class _FakeResult:
    def fetchone(self):
        return (1,)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.row_factory = None

    def execute(self, statement: str):
        if statement.startswith("PRAGMA "):
            return _FakeResult()
        raise AssertionError(f"unexpected non-read-only fixture SQL: {statement}")

    def close(self) -> None:
        self.closed = True


def _sqlite_error(code: int, name: str, message: str = "fixture SQLite error") -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class SQLiteReadOnlyRefreshTests(unittest.TestCase):
    def test_sidecar_identity_reports_replacement_without_reading_or_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "sessions.sqlite3"
            database.write_bytes(b"database")
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            journal = Path(f"{database}-journal")
            wal.write_bytes(b"old-wal")
            shm.write_bytes(b"old-shm")
            journal.write_bytes(b"old-journal")
            before = sqlite_read_only_state(database)
            replacement = root / "replacement-wal"
            replacement.write_bytes(b"new-wal")
            os.replace(replacement, wal)
            after = sqlite_read_only_state(database)

            self.assertTrue(sqlite_read_only_state_changed(before, after))
            self.assertEqual(wal.read_bytes(), b"new-wal")
            self.assertEqual(shm.read_bytes(), b"old-shm")
            self.assertEqual(journal.read_bytes(), b"old-journal")
            self.assertEqual(after.wal.path, str(wal))
            self.assertEqual(after.shm.path, str(shm))

    def test_transient_ioerr_reopens_after_wal_replacement_and_preserves_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "sessions.sqlite3"
            database.write_bytes(b"database")
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            wal.write_bytes(b"before")
            shm.write_bytes(b"shm")
            calls: list[int] = []
            sleeps: list[float] = []

            def opener(_path: Path) -> _FakeConnection:
                calls.append(1)
                if len(calls) == 1:
                    replacement = root / "replacement-wal"
                    replacement.write_bytes(b"after")
                    os.replace(replacement, wal)
                    raise _sqlite_error(
                        sqlite3.SQLITE_IOERR_READ,
                        "SQLITE_IOERR_READ",
                        "disk I/O error while rotating WAL",
                    )
                return _FakeConnection()

            result = read_only_refresh(
                database,
                lambda _connection: "read-only result",
                opener=opener,
                attempts=3,
                delay_seconds=0,
                sleep=sleeps.append,
            )

            self.assertEqual(result.value, "read-only result")
            self.assertEqual(result.attempts, 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(sleeps, [0])
            self.assertEqual(database.read_bytes(), b"database")
            self.assertEqual(wal.read_bytes(), b"after")
            self.assertEqual(shm.read_bytes(), b"shm")

    def test_persistent_notadb_is_reported_once_with_extended_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "sessions.sqlite3"
            database.write_bytes(b"database")
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            wal.write_bytes(b"wal")
            shm.write_bytes(b"shm")
            calls: list[int] = []

            def opener(_path: Path) -> _FakeConnection:
                calls.append(1)
                raise _sqlite_error(sqlite3.SQLITE_NOTADB, "SQLITE_NOTADB", "file is not a database")

            with self.assertRaises(SQLiteReadOnlyError) as caught:
                read_only_refresh(
                    database,
                    lambda _connection: None,
                    opener=opener,
                    attempts=3,
                    delay_seconds=0,
                )

            failure = caught.exception.failure
            self.assertEqual(len(calls), 1)
            self.assertFalse(failure.transient)
            self.assertEqual(failure.sqlite_extended_code, sqlite3.SQLITE_NOTADB)
            self.assertEqual(failure.sqlite_primary_code, sqlite3.SQLITE_NOTADB)
            self.assertIsNone(failure.sqlite_ioerr_subtype)
            self.assertEqual(failure.sqlite_error_name, "SQLITE_NOTADB")
            self.assertIn(str(database), failure.paths)
            self.assertIn(str(wal), failure.paths)
            self.assertIn(str(shm), failure.paths)
            self.assertEqual(database.read_bytes(), b"database")
            self.assertEqual(wal.read_bytes(), b"wal")
            self.assertEqual(shm.read_bytes(), b"shm")

    def test_exhausted_ioerr_keeps_extended_code_and_attempt_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "sessions.sqlite3"
            database.write_bytes(b"database")
            calls: list[int] = []

            def opener(_path: Path) -> _FakeConnection:
                calls.append(1)
                raise _sqlite_error(sqlite3.SQLITE_IOERR_READ, "SQLITE_IOERR_READ", "read failed")

            with self.assertRaises(SQLiteReadOnlyError) as caught:
                read_only_refresh(
                    database,
                    lambda _connection: None,
                    opener=opener,
                    attempts=3,
                    delay_seconds=0,
                )

            failure = caught.exception.failure
            self.assertEqual(len(calls), 3)
            self.assertTrue(failure.transient)
            self.assertEqual(failure.attempt, 3)
            self.assertEqual(failure.max_attempts, 3)
            self.assertEqual(failure.sqlite_extended_code, sqlite3.SQLITE_IOERR_READ)
            self.assertEqual(failure.sqlite_primary_code, sqlite3.SQLITE_IOERR)
            self.assertEqual(failure.sqlite_ioerr_subtype, sqlite3.SQLITE_IOERR_READ >> 8)
            self.assertEqual(failure.sqlite_ioerr_subtype_name, "SQLITE_IOERR_READ")
            self.assertIn("sqlite_errorcode=266", str(caught.exception))
            self.assertIn("ioerr_subtype=1", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
