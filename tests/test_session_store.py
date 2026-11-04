import errno
import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from unittest import mock

import termdeck.session_store as session_store_module
from termdeck.models import SessionRecord
from termdeck.session_store import (
    SessionDatabaseMigrationError,
    SessionDatabaseMigrationInProgress,
    SessionStore,
    default_auth_root,
    default_db_path,
    legacy_state_dir,
    migrate_legacy_database,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._test_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._test_home.cleanup)
        home = Path(self._test_home.name) / "home"
        home.mkdir()
        managed_root = home / ".local" / "state" / "t1000-managed"
        managed_state = managed_root.parent / "termdeck"
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "T1000_MANAGED_ROOT": str(managed_root),
                "TERMDECK_CODEX_ACCOUNT_SOURCE": str(home / ".codex"),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        defaults = mock.patch.multiple(
            session_store_module,
            APP_DIR=managed_state,
            DEFAULT_DB_PATH=managed_state / "sessions.sqlite3",
            DEFAULT_AUTH_ROOT=managed_state / "terminal-auth",
        )
        defaults.start()
        self.addCleanup(defaults.stop)
        process_fence = mock.patch.object(
            session_store_module,
            "_proc_process_directories",
            return_value=(Path("/proc") / str(os.getpid()),),
        )
        process_fence.start()
        self.addCleanup(process_fence.stop)

    @staticmethod
    def _guard_sqlite_source(legacy: Path):
        source_metadata = legacy.stat()
        source_identity = (
            int(source_metadata.st_dev),
            int(source_metadata.st_ino),
        )
        real_connect = sqlite3.connect

        def guarded_connect(database, *args, **kwargs):
            database_text = os.fsdecode(os.fspath(database))
            candidate_text = database_text
            if database_text.startswith("file:"):
                parsed = urlsplit(database_text)
                if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                    return real_connect(database, *args, **kwargs)
                candidate_text = unquote(parsed.path)
            try:
                metadata = Path(candidate_text).stat()
            except OSError:
                pass
            else:
                if (int(metadata.st_dev), int(metadata.st_ino)) == source_identity:
                    raise AssertionError(
                        "migration SQLite-opened the live legacy source"
                    )
            return real_connect(database, *args, **kwargs)

        return guarded_connect

    def test_default_paths_are_fenced_inside_the_test_home(self):
        home = Path(os.environ["HOME"])
        self.assertTrue(default_db_path().is_relative_to(home))
        self.assertTrue(default_auth_root().is_relative_to(home))
        self.assertTrue(legacy_state_dir().is_relative_to(home))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("T1000_MANAGED_ROOT", None)
            legacy_root = home / ".local" / "share" / "termdeck"
            self.assertEqual(default_db_path(), legacy_root / "sessions.sqlite3")
            self.assertEqual(default_auth_root(), legacy_root / "terminal-auth")

    def test_sqlite_source_guard_rejects_path_uri_proc_and_hardlink_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy.sqlite3"
            connection = sqlite3.connect(legacy)
            connection.close()
            alias = root / "alias.sqlite3"
            os.link(legacy, alias)
            descriptor = os.open(legacy, os.O_RDONLY)
            self.addCleanup(os.close, descriptor)
            guarded_connect = self._guard_sqlite_source(legacy)

            for candidate in (
                str(legacy),
                legacy.as_uri(),
                str(alias),
                f"/proc/self/fd/{descriptor}",
                Path(f"/proc/self/fd/{descriptor}").as_uri(),
            ):
                with self.subTest(candidate=candidate), self.assertRaisesRegex(
                    AssertionError,
                    "SQLite-opened the live legacy source",
                ):
                    guarded_connect(candidate)

    def test_managed_defaults_use_sibling_termdeck_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "t1000-managed"
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ):
                expected_root = managed_root.parent / "termdeck"
                self.assertEqual(default_db_path(), expected_root / "sessions.sqlite3")
                self.assertEqual(default_auth_root(), expected_root / "terminal-auth")
                store = SessionStore(provision_auth_scopes=True)
                try:
                    self.assertEqual(store.path, expected_root / "sessions.sqlite3")
                    self.assertEqual(store.auth_root, expected_root / "terminal-auth")
                    self.assertEqual(
                        store.path.stat().st_mode & 0o777,
                        0o600,
                    )
                    self.assertEqual(
                        expected_root.stat().st_mode & 0o777,
                        0o700,
                    )
                    self.assertEqual(
                        store.auth_root.stat().st_mode & 0o777,
                        0o700,
                    )
                    for suffix in ("-wal", "-shm"):
                        sidecar = store.path.with_name(f"{store.path.name}{suffix}")
                        if sidecar.exists():
                            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
                finally:
                    store.close()

    def test_explicit_database_path_remains_authoritative_under_managed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit" / "sessions.sqlite3"
            managed_root = Path(tmp) / "t1000-managed"
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ):
                store = SessionStore(explicit)
                try:
                    self.assertEqual(store.path, explicit)
                    self.assertEqual(store.auth_root, explicit.parent / "terminal-auth")
                    self.assertFalse(store.auth_root.exists())
                finally:
                    store.close()

    def test_legacy_database_migration_is_atomic_and_does_not_copy_auth_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            try:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker(value) VALUES ('legacy')")
                connection.commit()
            finally:
                connection.close()
            (legacy.parent / "terminal-auth").mkdir()
            (legacy.parent / "terminal-auth" / "not-copied").write_text("x")
            managed_root = root / "managed"
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ):
                result = migrate_legacy_database(legacy_path=legacy)
                destination = managed_root.parent / "termdeck" / "sessions.sqlite3"
                self.assertEqual(result.status, "migrated")
                self.assertEqual(result.source, legacy.resolve())
                self.assertEqual(result.destination, destination.resolve())
                self.assertTrue(destination.is_file())
                self.assertFalse((destination.parent / "terminal-auth").exists())
                migrated = sqlite3.connect(destination)
                try:
                    self.assertEqual(
                        migrated.execute("SELECT value FROM marker").fetchone()[0],
                        "legacy",
                    )
                finally:
                    migrated.close()
                before = destination.read_bytes()
                self.assertIsNotNone(result.receipt_path)
                self.assertTrue(result.receipt_path.is_file())  # type: ignore[union-attr]
                repeated = migrate_legacy_database(legacy_path=legacy)
                self.assertEqual(repeated.status, "verified")
                self.assertEqual(repeated.database_id, result.database_id)
                self.assertEqual(destination.read_bytes(), before)

    def test_stale_empty_managed_database_is_repaired_with_parity_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy_store = SessionStore(legacy)
            first = legacy_store.create(
                "one",
                "dev",
                "codex",
                root,
                window_id="window-one",
                window_title="One",
            )
            second = legacy_store.create(
                "two",
                "dev",
                "bash",
                root,
                window_id="window-two",
                window_title="Two",
            )
            legacy_store.remember_window("window-one", "One")
            legacy_store.remember_window("window-two", "Two")
            legacy_store.set_window_captain("window-one", first.id)
            legacy_store.close()
            source_before = {
                path.name: path.read_bytes()
                for path in legacy.parent.iterdir()
                if path.is_file()
            }

            managed_root = root / "managed" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            stale_store = SessionStore(target)
            stale_store.close()

            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ):
                result = migrate_legacy_database(
                    destination=target,
                    legacy_path=legacy,
                )

                self.assertEqual(result.status, "repaired_existing")
                self.assertIsNotNone(result.backup_path)
                self.assertTrue((result.backup_path / "logical.sqlite3").is_file())  # type: ignore[operator]
                self.assertFalse((result.backup_path / "original.sqlite3-shm").exists())  # type: ignore[operator]
                repaired = SessionStore(target)
                try:
                    self.assertEqual(
                        {record.id for record in repaired.list()},
                        {first.id, second.id},
                    )
                    self.assertEqual(
                        repaired.remembered_windows(),
                        [("window-one", "One"), ("window-two", "Two")],
                    )
                    self.assertEqual(
                        repaired.captain_for_window("window-one").id,  # type: ignore[union-attr]
                        first.id,
                    )
                finally:
                    repaired.close()

                with mock.patch(
                    "termdeck.session_store._migration_source_parent",
                    side_effect=AssertionError("sealed restart touched legacy state"),
                ):
                    repeated = migrate_legacy_database(
                        destination=target,
                        legacy_path=legacy,
                    )
                self.assertEqual(repeated.status, "verified")
                self.assertEqual(repeated.database_id, result.database_id)

            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in legacy.parent.iterdir()
                    if path.is_file()
                },
                source_before,
            )

    @staticmethod
    def _stale_migration_pair(root: Path) -> tuple[Path, Path]:
        legacy = root / "legacy" / "sessions.sqlite3"
        target = root / "managed" / "sessions.sqlite3"
        legacy_store = SessionStore(legacy)
        try:
            legacy_store.create("legacy", "dev", "codex", root)
        finally:
            legacy_store.close()
        stale_store = SessionStore(target)
        stale_store.close()
        return legacy, target

    def test_empty_wal_ctime_transition_is_validated_under_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = self._stale_migration_pair(Path(tmp))
            real_open = session_store_module._open_exclusive_database
            observed = []

            def open_and_check_lock(destination):
                wal = target.with_name(f"{target.name}-wal")
                before = wal.stat()
                connection = real_open(destination)
                self.addCleanup(connection.close)
                self.assertEqual(
                    connection.execute("PRAGMA locking_mode").fetchone()[0],
                    "exclusive",
                )
                competitor = sqlite3.connect(target, timeout=0.0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competitor.execute("BEGIN IMMEDIATE")
                finally:
                    competitor.close()
                # Deterministically reproduce SQLite's metadata-only WAL touch.
                os.chmod(wal, before.st_mode & 0o777)
                after = wal.stat()
                self.assertEqual(wal.read_bytes(), b"")
                self.assertEqual(
                    session_store_module._file_signature(before)[:4],
                    session_store_module._file_signature(after)[:4],
                )
                self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
                observed.append((before.st_ctime_ns, after.st_ctime_ns))
                return connection

            with mock.patch.object(
                session_store_module,
                "_open_exclusive_database",
                side_effect=open_and_check_lock,
            ):
                result = migrate_legacy_database(destination=target, legacy_path=legacy)
            self.assertEqual(result.status, "repaired_existing")
            self.assertEqual(len(observed), 1)
            repaired = SessionStore(target)
            try:
                self.assertEqual([record.name for record in repaired.list()], ["legacy"])
            finally:
                repaired.close()

    def test_migration_factories_close_descriptors_on_initialization_error(self):
        faults = (
            "regular-fstat", "snapshot-fstat", "snapshot-hash",
            "sidecar-hash", "backup-fstat", "snapshot-hash-close-error",
            "regular-fstat-close-error", "backup-fstat-close-error", "sidecar-hash-close-error",
        )
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "sessions.sqlite3"
                source.write_bytes(b"owned descriptor fault fixture")
                source.chmod(0o600)
                source.with_name(f"{source.name}-wal").write_bytes(b"")
                parent = session_store_module._open_pinned_directory(root, create=False)
                real_open, real_fstat, real_close = os.open, os.fstat, os.close
                real_hash = session_store_module._sha256_descriptor
                opened = []
                stat_calls = {}
                hash_calls = []
                failure = OSError(errno.EIO, "injected initialization failure")

                def capture_open(*args, **kwargs):
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                def failing_stat(descriptor):
                    stat_calls[descriptor] = stat_calls.get(descriptor, 0) + 1
                    if descriptor in opened and (
                        fault.startswith(("regular-fstat", "backup-fstat"))
                        or (fault == "snapshot-fstat" and stat_calls[descriptor] == 2)
                    ):
                        raise failure
                    return real_fstat(descriptor)

                def failing_hash(descriptor):
                    hash_calls.append(descriptor)
                    if fault in {"snapshot-hash", "snapshot-hash-close-error"} or (
                        fault.startswith("sidecar-hash") and len(hash_calls) == 2
                    ):
                        raise failure
                    return real_hash(descriptor)

                def failing_close(descriptor):
                    real_close(descriptor)
                    if fault.endswith("close-error"):
                        raise OSError(errno.EBADF, "injected secondary close failure")

                try:
                    with mock.patch.object(session_store_module.os, "open", side_effect=capture_open), \
                        mock.patch.object(session_store_module.os, "fstat", side_effect=failing_stat), \
                        mock.patch.object(session_store_module.os, "close", side_effect=failing_close), \
                        mock.patch.object(session_store_module, "_sha256_descriptor", side_effect=failing_hash):
                        with self.assertRaises(OSError) as observed:
                            if fault.startswith("regular-fstat"):
                                session_store_module._open_regular_at(
                                    parent, source.name, label="owned test fixture", required=True,
                                )
                            elif fault.startswith("backup-fstat"):
                                session_store_module._create_backup_directory(parent, source)
                            else:
                                session_store_module._open_legacy_snapshot_files(source, parent)
                    self.assertIs(observed.exception, failure)
                    self.assertGreater(len(opened), 0)
                    for descriptor in opened:
                        with self.assertRaises(OSError) as closed:
                            real_fstat(descriptor)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                finally:
                    # Red-before runs must also release only their own leaked fds.
                    for descriptor in opened:
                        try:
                            real_close(descriptor)
                        except OSError:
                            pass
                    parent.close()

    def test_exclusive_open_rejects_mutated_database_files_before_replacement(self):
        faults = (
            "wal-replace", "wal-symlink", "wal-write", "wal-mtime",
            "wal-mode", "wal-hardlink", "main-replace", "main-write",
            "main-ctime", "shm-ctime", "journal-appeared",
        )
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                legacy, target = self._stale_migration_pair(root)
                legacy_before = legacy.read_bytes()
                real_open = session_store_module._open_exclusive_database
                injected = []

                def mutate_after_lock(destination):
                    connection = real_open(destination)
                    self.addCleanup(connection.close)
                    wal = target.with_name(f"{target.name}-wal")
                    before = wal.stat()
                    self.assertEqual(wal.read_bytes(), b"")
                    if fault in {"wal-replace", "wal-symlink"}:
                        moved = wal.with_name("old-wal")
                        wal.replace(moved)
                        if fault == "wal-replace":
                            wal.write_bytes(b"")
                            wal.chmod(0o600)
                        else:
                            wal.symlink_to(moved)
                    elif fault == "wal-write":
                        wal.write_bytes(b"external writer")
                        os.utime(wal, ns=(before.st_atime_ns, before.st_mtime_ns))
                    elif fault == "wal-mtime":
                        os.utime(wal, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
                    elif fault == "wal-mode":
                        wal.chmod(0o666)
                    elif fault == "wal-hardlink":
                        os.link(wal, root / "wal-alias")
                    elif fault == "main-replace":
                        before_main = target.read_bytes()
                        target.replace(root / "old-main")
                        target.write_bytes(before_main)
                        target.chmod(0o600)
                    elif fault == "main-write":
                        before_main = target.stat()
                        content = target.read_bytes()
                        target.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
                        os.utime(target, ns=(before_main.st_atime_ns, before_main.st_mtime_ns))
                    elif fault == "main-ctime":
                        target.chmod(target.stat().st_mode & 0o777)
                    elif fault == "shm-ctime":
                        shm = target.with_name(f"{target.name}-shm")
                        shm.chmod(shm.stat().st_mode & 0o777)
                    else:
                        target.with_name(f"{target.name}-journal").write_bytes(b"")
                    injected.append(target.read_bytes())
                    return connection

                with mock.patch.object(
                    session_store_module, "_open_exclusive_database",
                    side_effect=mutate_after_lock,
                ), mock.patch.object(
                    session_store_module, "_replace_database_from_snapshot",
                ) as replace_database, self.assertRaises(SessionDatabaseMigrationError):
                    migrate_legacy_database(destination=target, legacy_path=legacy)
                replace_database.assert_not_called()
                self.assertEqual(len(injected), 1)
                self.assertEqual(target.read_bytes(), injected[0])
                self.assertEqual(legacy.read_bytes(), legacy_before)
                self.assertFalse(target.with_name(f".{target.name}.migration-receipt.json").exists())

    def test_empty_wal_foreign_owner_and_backward_ctime_are_rejected(self):
        for fault in ("foreign-owner", "backward-ctime"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                legacy, target = self._stale_migration_pair(Path(tmp))
                real_open = session_store_module._open_exclusive_database
                real_fstat = os.fstat
                changed_identity = []

                def open_with_changed_metadata(destination):
                    wal = target.with_name(f"{target.name}-wal")
                    before = wal.stat()
                    connection = real_open(destination)
                    self.addCleanup(connection.close)
                    changed_identity.append((before.st_dev, before.st_ino, before.st_ctime_ns))
                    return connection

                def changed_fstat(descriptor):
                    metadata = real_fstat(descriptor)
                    for device, inode, ctime in changed_identity:
                        if (metadata.st_dev, metadata.st_ino) != (device, inode):
                            continue
                        values = {
                            field: getattr(metadata, field)
                            for field in (
                                "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns",
                                "st_mode", "st_uid", "st_gid", "st_nlink",
                            )
                        }
                        if fault == "foreign-owner":
                            values["st_uid"] = os.geteuid() + 1
                        else:
                            values["st_ctime_ns"] = ctime - 1
                        return SimpleNamespace(**values)
                    return metadata

                with mock.patch.object(
                    session_store_module, "_open_exclusive_database",
                    side_effect=open_with_changed_metadata,
                ), mock.patch.object(
                    session_store_module.os, "fstat", side_effect=changed_fstat,
                ), mock.patch.object(
                    session_store_module, "_replace_database_from_snapshot",
                ) as replace_database, self.assertRaises(SessionDatabaseMigrationError):
                    migrate_legacy_database(destination=target, legacy_path=legacy)
                replace_database.assert_not_called()
                self.assertEqual(len(changed_identity), 1)

    def test_nonempty_wal_ctime_change_is_never_rebased(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, target = self._stale_migration_pair(root)
            origin = SessionStore(root / "origin" / "sessions.sqlite3")
            try:
                for suffix in ("", "-wal", "-shm"):
                    component = origin.path.with_name(f"{origin.path.name}{suffix}")
                    copied = target.with_name(f"{target.name}{suffix}")
                    copied.write_bytes(component.read_bytes())
                    copied.chmod(0o600)
            finally:
                origin.close()
            real_open = session_store_module._open_exclusive_database
            touched = []

            def touch_nonempty_wal(destination):
                wal = target.with_name(f"{target.name}-wal")
                before = wal.stat()
                self.assertGreater(before.st_size, 0)
                connection = real_open(destination)
                self.addCleanup(connection.close)
                wal.chmod(before.st_mode & 0o777)
                touched.append(True)
                return connection

            with mock.patch.object(
                session_store_module, "_open_exclusive_database",
                side_effect=touch_nonempty_wal,
            ), mock.patch.object(
                session_store_module, "_replace_database_from_snapshot",
            ) as replace_database, self.assertRaises(SessionDatabaseMigrationError):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            replace_database.assert_not_called()
            self.assertEqual(touched, [True])

    def test_target_change_before_exclusive_probe_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = self._stale_migration_pair(Path(tmp))
            target_inode = target.stat().st_ino
            real_quiescent = session_store_module._assert_legacy_snapshot_quiescent
            checks = []

            def mutate_before_probe(files):
                real_quiescent(files)
                if files[""].signature[1] == target_inode:
                    checks.append(True)
                    if len(checks) == 2:
                        wal = target.with_name(f"{target.name}-wal")
                        metadata = wal.stat()
                        os.utime(wal, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1000000))

            with mock.patch.object(
                session_store_module, "_assert_legacy_snapshot_quiescent",
                side_effect=mutate_before_probe,
            ), mock.patch.object(
                session_store_module, "_open_exclusive_database",
            ) as open_database, self.assertRaises(SessionDatabaseMigrationError):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            open_database.assert_not_called()
            self.assertEqual(len(checks), 2)

    def test_repaired_target_swap_after_replacement_fails_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, target = self._stale_migration_pair(root)
            source_before = legacy.read_bytes()
            target_before = target.read_bytes()
            prior = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
            prior.row_factory = sqlite3.Row
            try:
                prior_summary = session_store_module._database_summary(prior)
            finally:
                prior.close()
            moved = root / "original-target.sqlite3"
            real_replace = session_store_module._replace_database_from_snapshot
            calls = []

            def replace_and_swap(snapshot, connection):
                summary = real_replace(snapshot, connection)
                calls.append(True)
                if len(calls) == 1:
                    target.replace(moved)
                    target.write_bytes(target_before)
                    target.chmod(0o600)
                return summary

            with mock.patch.object(
                session_store_module, "_replace_database_from_snapshot",
                side_effect=replace_and_swap,
            ), self.assertRaisesRegex(SessionDatabaseMigrationError, "identity changed"):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            self.assertEqual(len(calls), 2)  # replacement then rollback of the bound inode
            self.assertEqual(target.read_bytes(), target_before)
            restored = sqlite3.connect(moved.as_uri() + "?mode=ro", uri=True)
            restored.row_factory = sqlite3.Row
            try:
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(session_store_module._database_summary(restored), prior_summary)
            finally:
                restored.close()
            self.assertEqual(legacy.read_bytes(), source_before)
            self.assertFalse(target.with_name(f".{target.name}.migration-receipt.json").exists())

    def test_empty_wal_ctime_change_after_lock_validation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = self._stale_migration_pair(Path(tmp))
            real_marker = session_store_module._install_migration_marker
            touched = []

            def touch_after_validation(snapshot, **kwargs):
                real_marker(snapshot, **kwargs)
                wal = target.with_name(f"{target.name}-wal")
                wal.chmod(wal.stat().st_mode & 0o777)
                touched.append(True)

            with mock.patch.object(
                session_store_module, "_install_migration_marker",
                side_effect=touch_after_validation,
            ), mock.patch.object(
                session_store_module, "_replace_database_from_snapshot",
            ) as replace_database, self.assertRaises(SessionDatabaseMigrationError):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            replace_database.assert_not_called()
            self.assertEqual(touched, [True])

    def test_empty_wal_repair_requires_exclusive_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = self._stale_migration_pair(Path(tmp))
            opened = []

            def open_without_exclusive_lock(destination):
                connection = sqlite3.connect(str(destination), timeout=0.0)
                connection.row_factory = sqlite3.Row
                self.addCleanup(connection.close)
                opened.append(True)
                return connection

            with mock.patch.object(
                session_store_module, "_open_exclusive_database",
                side_effect=open_without_exclusive_lock,
            ), mock.patch.object(
                session_store_module, "_replace_database_from_snapshot",
            ) as replace_database, self.assertRaises(SessionDatabaseMigrationError):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            replace_database.assert_not_called()
            self.assertEqual(opened, [True])

    def test_concurrent_managed_writer_prevents_empty_wal_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = self._stale_migration_pair(Path(tmp))
            real_open = session_store_module._open_exclusive_database
            writers = []
            child = (
                "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
                "c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); "
                "sys.stdin.readline(); c.rollback(); c.close()"
            )

            def open_with_competing_writer(destination):
                writer = subprocess.Popen(
                    [sys.executable, "-c", child, str(target)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                writers.append(writer)
                try:
                    ready, _, _ = select.select([writer.stdout], [], [], 5)
                    self.assertTrue(ready, "disposable writer did not signal readiness")
                    self.assertEqual(writer.stdout.readline().strip(), "locked")
                    return real_open(destination)
                finally:
                    try:
                        writer.communicate("release\n", timeout=5)
                    except subprocess.TimeoutExpired:
                        writer.kill()
                        writer.communicate(timeout=5)
                        raise
                    self.assertEqual(writer.returncode, 0)

            with mock.patch.object(
                session_store_module, "_open_exclusive_database",
                side_effect=open_with_competing_writer,
            ), mock.patch.object(
                session_store_module, "_replace_database_from_snapshot",
            ) as replace_database, self.assertRaisesRegex(
                SessionDatabaseMigrationError, "changed or became busy",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            replace_database.assert_not_called()
            self.assertEqual(len(writers), 1)

    def test_divergent_managed_database_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            target = root / "managed" / "sessions.sqlite3"
            legacy_store = SessionStore(legacy)
            legacy_store.create("legacy", "dev", "codex", root)
            legacy_store.close()
            target_store = SessionStore(target)
            target_store.create("managed-only", "dev", "bash", root)
            target_store.close()
            source_before = legacy.read_bytes()
            target_before = target.read_bytes()

            with self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "diverges from the legacy registry",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

            self.assertEqual(legacy.read_bytes(), source_before)
            self.assertEqual(target.read_bytes(), target_before)
            self.assertEqual(
                list(target.parent.glob(".sessions.sqlite3.pre-migration-*")),
                [],
            )

    def test_live_wal_writer_blocks_migration_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            writer = sqlite3.connect(legacy)
            try:
                self.assertEqual(
                    str(writer.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower(),
                    "wal",
                )
                writer.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
                writer.execute("INSERT INTO marker(value) VALUES ('held-open')")
                writer.commit()
                source_before = {
                    path.name: path.read_bytes()
                    for path in legacy.parent.iterdir()
                    if path.is_file()
                }
                with self.assertRaisesRegex(
                    SessionDatabaseMigrationError,
                    "legacy session database is still open",
                ):
                    migrate_legacy_database(
                        destination=root / "managed" / "sessions.sqlite3",
                        legacy_path=legacy,
                    )
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in legacy.parent.iterdir()
                        if path.is_file()
                    },
                    source_before,
                )
            finally:
                writer.close()

    def test_closed_wal_snapshot_migrates_without_opening_live_legacy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.sqlite3"
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            writer = sqlite3.connect(origin)
            try:
                self.assertEqual(
                    str(writer.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower(),
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
                writer.execute("INSERT INTO marker(value) VALUES ('from-wal')")
                writer.commit()
                for suffix in ("", "-wal", "-shm"):
                    source_file = origin.with_name(f"{origin.name}{suffix}")
                    if source_file.exists():
                        legacy.with_name(f"{legacy.name}{suffix}").write_bytes(
                            source_file.read_bytes()
                        )
            finally:
                writer.close()

            source_before = {
                path.name: path.read_bytes()
                for path in legacy.parent.iterdir()
                if path.is_file()
            }
            target = root / "managed" / "sessions.sqlite3"
            with mock.patch(
                "termdeck.session_store.sqlite3.connect",
                side_effect=self._guard_sqlite_source(legacy),
            ):
                result = migrate_legacy_database(
                    destination=target,
                    legacy_path=legacy,
                )

            self.assertEqual(result.status, "migrated")
            migrated = sqlite3.connect(target)
            try:
                self.assertEqual(
                    migrated.execute("SELECT value FROM marker").fetchone()[0],
                    "from-wal",
                )
            finally:
                migrated.close()
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in legacy.parent.iterdir()
                    if path.is_file()
                },
                source_before,
            )

    def test_invalid_legacy_database_never_publishes_a_partial_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            legacy.write_bytes(b"not-a-sqlite-database")
            managed_root = root / "managed"
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), self.assertRaisesRegex(
                RuntimeError,
                "SQLite integrity check failed|not a readable SQLite database",
            ):
                migrate_legacy_database(legacy_path=legacy)
            destination = managed_root.parent / "termdeck" / "sessions.sqlite3"
            self.assertFalse(destination.exists())
            self.assertEqual(
                list(destination.parent.glob(".sessions.sqlite3.migration-*")),
                [],
            )

    def test_concurrent_migration_fails_immediately_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.commit()
            connection.close()
            managed_root = root / "managed"
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch(
                "termdeck.session_store.fcntl.flock",
                side_effect=BlockingIOError,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationInProgress,
                "MIGRATION_IN_PROGRESS",
            ):
                migrate_legacy_database(legacy_path=legacy)

    def test_migration_rejects_lock_path_swap_after_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            lock_path = target.parent / ".sessions.sqlite3.migration.lock"
            moved_lock = target.parent / ".moved-migration.lock"
            real_flock = session_store_module.fcntl.flock

            def swap_lock_path(descriptor, operation):
                real_flock(descriptor, operation)
                os.replace(lock_path, moved_lock)
                lock_path.write_bytes(b"")
                lock_path.chmod(0o600)

            with mock.patch(
                "termdeck.session_store.fcntl.flock",
                side_effect=swap_lock_path,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "migration lock is unsafe",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

            self.assertFalse(target.exists())

    def test_migration_rejects_same_path_and_hardlink_alias_without_sqlite_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy.sqlite3"
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()

            with mock.patch(
                "termdeck.session_store.sqlite3.connect",
                side_effect=AssertionError("unexpected SQLite open"),
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "paths must be distinct",
            ):
                migrate_legacy_database(destination=legacy, legacy_path=legacy)

            alias = root / "managed.sqlite3"
            os.link(legacy, alias)
            with mock.patch(
                "termdeck.session_store.sqlite3.connect",
                side_effect=AssertionError("unexpected SQLite open"),
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "files must be distinct",
            ):
                migrate_legacy_database(destination=alias, legacy_path=legacy)

    def test_unsealed_target_and_orphaned_receipt_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "managed" / "sessions.sqlite3"
            target.parent.mkdir()
            connection = sqlite3.connect(target)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.commit()
            connection.close()
            missing_source = root / "missing" / "sessions.sqlite3"

            with self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "unverified managed session database",
            ):
                migrate_legacy_database(
                    destination=target,
                    legacy_path=missing_source,
                )

            target.unlink()
            receipt = target.parent / ".sessions.sqlite3.migration-receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            receipt.chmod(0o600)
            with self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "database is missing",
            ):
                migrate_legacy_database(
                    destination=target,
                    legacy_path=missing_source,
                )

            receipt.unlink()
            os.mkfifo(receipt, 0o600)
            with self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "receipt is unsafe",
            ):
                migrate_legacy_database(
                    destination=target,
                    legacy_path=missing_source,
                )

    def test_migration_preserves_304_sessions_17_windows_and_9_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            target = root / "managed" / "sessions.sqlite3"
            store = SessionStore(legacy)
            records = []
            try:
                for index in range(304):
                    window_index = index % 17
                    records.append(
                        store.create(
                            f"session-{index:03d}",
                            "migration",
                            "bash",
                            root,
                            window_id=f"window-{window_index:02d}",
                            window_title=f"Window {window_index:02d}",
                            tab_index=index // 17,
                        )
                    )
                store.conn.execute("DELETE FROM remembered_windows")
                for index in range(9):
                    window_id = f"window-{index:02d}"
                    store.remember_window(window_id, f"Window {index:02d}")
                    store.set_window_captain(window_id, records[index].id)
                store.conn.execute("PRAGMA user_version = 23")
                store.conn.commit()
            finally:
                store.close()

            source = sqlite3.connect(legacy)
            try:
                source_rows = source.execute(
                    "SELECT * FROM sessions ORDER BY id"
                ).fetchall()
                source_windows = source.execute(
                    "SELECT * FROM remembered_windows ORDER BY window_id"
                ).fetchall()
                source_schema = source.execute(
                    """
                    SELECT type, name, tbl_name, COALESCE(sql, '')
                    FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    ORDER BY type, name, tbl_name, sql
                    """
                ).fetchall()
            finally:
                source.close()

            with mock.patch(
                "termdeck.session_store.sqlite3.connect",
                side_effect=self._guard_sqlite_source(legacy),
            ):
                result = migrate_legacy_database(
                    destination=target,
                    legacy_path=legacy,
                )

            self.assertEqual(result.status, "migrated")
            receipt_document = json.loads(result.receipt_path.read_text())  # type: ignore[union-attr]
            source_summary = receipt_document["source_summary"]
            self.assertEqual(source_summary["session_count"], 304)
            self.assertEqual(source_summary["distinct_window_count"], 17)
            self.assertEqual(source_summary["remembered_window_count"], 9)
            self.assertEqual(receipt_document["destination_summary"], source_summary)
            self.assertEqual(receipt_document["source_file_sha256"]["main"], result.source_sha256)

            migrated = sqlite3.connect(target)
            try:
                self.assertEqual(
                    migrated.execute("SELECT * FROM sessions ORDER BY id").fetchall(),
                    source_rows,
                )
                self.assertEqual(
                    migrated.execute(
                        "SELECT * FROM remembered_windows ORDER BY window_id"
                    ).fetchall(),
                    source_windows,
                )
                self.assertEqual(
                    migrated.execute("SELECT COUNT(DISTINCT window_id) FROM sessions").fetchone()[0],
                    17,
                )
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 23)
                target_schema = migrated.execute(
                    """
                    SELECT type, name, tbl_name, COALESCE(sql, '')
                    FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                      AND name != 'termdeck_migration_state'
                      AND tbl_name != 'termdeck_migration_state'
                    ORDER BY type, name, tbl_name, sql
                    """
                ).fetchall()
                self.assertEqual(target_schema, source_schema)
            finally:
                migrated.close()

            target_before = target.read_bytes()
            for path in legacy.parent.iterdir():
                path.unlink()
            legacy.parent.rmdir()
            repeated = migrate_legacy_database(
                destination=target,
                legacy_path=legacy,
            )
            self.assertEqual(repeated.status, "verified")
            self.assertEqual(repeated.database_id, result.database_id)
            self.assertEqual(repeated.destination_sha256, result.destination_sha256)
            self.assertEqual(target.read_bytes(), target_before)

    def test_source_change_during_publish_rolls_back_new_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute(
                "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO marker(value) VALUES ('before')")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            real_link = os.link

            def mutate_source_then_link(*args, **kwargs):
                with legacy.open("ab") as writer:
                    writer.write(b"raw-tamper")
                    writer.flush()
                    os.fsync(writer.fileno())
                return real_link(*args, **kwargs)

            with mock.patch(
                "termdeck.session_store.os.link",
                side_effect=mutate_source_then_link,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "changed during migration",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

            self.assertFalse(target.exists())
            self.assertFalse(
                (target.parent / ".sessions.sqlite3.migration-receipt.json").exists()
            )

    def test_crash_before_receipt_commit_revalidates_source_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute(
                "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO marker(value) VALUES ('before-crash')")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            receipt = target.parent / ".sessions.sqlite3.migration-receipt.json"
            read_descriptor, write_descriptor = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_descriptor)

                def crash_before_receipt(parent, name, content):
                    del parent, name, content
                    os.write(write_descriptor, b"C")
                    os._exit(73)

                with mock.patch.object(
                    session_store_module,
                    "_write_private_at",
                    side_effect=crash_before_receipt,
                ), mock.patch.object(
                    session_store_module,
                    "_proc_process_directories",
                    return_value=(Path("/proc") / str(os.getpid()),),
                ):
                    migrate_legacy_database(destination=target, legacy_path=legacy)
                os._exit(74)

            os.close(write_descriptor)
            try:
                self.assertEqual(os.read(read_descriptor, 1), b"C")
            finally:
                os.close(read_descriptor)
            waited_pid, status = os.waitpid(child_pid, 0)
            self.assertEqual(waited_pid, child_pid)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 73)
            self.assertTrue(target.is_file())
            self.assertFalse(receipt.exists())

            source = sqlite3.connect(legacy)
            source.execute("INSERT INTO marker(value) VALUES ('after-crash')")
            source.commit()
            source.close()

            retried = migrate_legacy_database(destination=target, legacy_path=legacy)

            self.assertEqual(retried.status, "repaired_existing")
            self.assertTrue(receipt.is_file())
            migrated = sqlite3.connect(target)
            try:
                self.assertEqual(
                    migrated.execute("SELECT value FROM marker ORDER BY id").fetchall(),
                    [("before-crash",), ("after-crash",)],
                )
            finally:
                migrated.close()

    def test_ofd_fence_survives_same_process_open_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute(
                "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO marker(value) VALUES ('initial')")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            real_link = os.link
            writer_script = """
import sqlite3
import sys

try:
    connection = sqlite3.connect(sys.argv[1], timeout=0.0)
    connection.execute("INSERT INTO marker(value) VALUES ('child')")
    connection.commit()
except sqlite3.OperationalError:
    raise SystemExit(23)
finally:
    try:
        connection.close()
    except NameError:
        pass
"""
            observed = []

            def probe_fence_then_link(*args, **kwargs):
                same_process = sqlite3.connect(legacy, timeout=0.0)
                same_process.close()
                child = subprocess.run(
                    [sys.executable, "-c", writer_script, str(legacy)],
                    check=False,
                    timeout=10,
                )
                observed.append(child.returncode)
                return real_link(*args, **kwargs)

            with mock.patch(
                "termdeck.session_store.os.link",
                side_effect=probe_fence_then_link,
            ):
                result = migrate_legacy_database(
                    destination=target,
                    legacy_path=legacy,
                )

            self.assertEqual(result.status, "migrated")
            self.assertEqual(observed, [23])
            source = sqlite3.connect(legacy)
            try:
                self.assertEqual(source.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)
            finally:
                source.close()

    def test_nondumpable_legacy_holder_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.commit()
            connection.close()
            holder_script = """
import ctypes
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
    raise SystemExit(2)
print("READY", flush=True)
sys.stdin.readline()
connection.close()
"""
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script, str(legacy)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                ready, _, _ = select.select([holder.stdout], [], [], 5)
                self.assertTrue(ready, "disposable holder did not signal readiness")
                self.assertEqual(holder.stdout.readline().strip(), "READY")  # type: ignore[union-attr]
                with mock.patch.object(
                    session_store_module,
                    "_proc_process_directories",
                    return_value=(
                        Path("/proc") / str(os.getpid()),
                        Path("/proc") / str(holder.pid),
                    ),
                ):
                    with self.assertRaisesRegex(
                        SessionDatabaseMigrationError,
                        "cannot prove.*quiescent",
                    ):
                        migrate_legacy_database(
                            destination=root / "managed" / "sessions.sqlite3",
                            legacy_path=legacy,
                        )
            finally:
                try:
                    holder.communicate("\n", timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate(timeout=5)
                    raise

    def test_copied_component_hash_mismatch_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            alternate = root / "alternate.sqlite3"
            legacy.parent.mkdir()
            for path, value in ((legacy, "legacy"), (alternate, "alternate")):
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker(value) VALUES (?)", (value,))
                connection.commit()
                connection.close()
            target = root / "managed" / "sessions.sqlite3"
            real_copy = session_store_module._copy_descriptor
            replaced = False

            def replace_first_copy(descriptor, destination):
                nonlocal replaced
                real_copy(descriptor, destination)
                if not replaced:
                    replaced = True
                    Path(destination).write_bytes(alternate.read_bytes())

            with mock.patch(
                "termdeck.session_store._copy_descriptor",
                side_effect=replace_first_copy,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "descriptor hash proof",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            self.assertFalse(target.exists())

    def test_verified_receipt_rejects_target_swap_during_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker(value) VALUES ('legacy')")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            migrate_legacy_database(destination=target, legacy_path=legacy)
            decoy = root / "decoy.sqlite3"
            connection = sqlite3.connect(decoy)
            connection.execute("CREATE TABLE decoy (value TEXT)")
            connection.commit()
            connection.close()
            original = target.parent / "original.sqlite3"
            real_hash = session_store_module._sha256_descriptor
            swapped = False

            def swap_during_hash(descriptor):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.replace(target, original)
                    os.replace(decoy, target)
                return real_hash(descriptor)

            with mock.patch(
                "termdeck.session_store._sha256_descriptor",
                side_effect=swap_during_hash,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "identity changed|changed during receipt",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

    def test_verified_receipt_rejects_hardlink_and_midcheck_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            migrated = migrate_legacy_database(destination=target, legacy_path=legacy)
            assert migrated.receipt_path is not None
            receipt = migrated.receipt_path
            receipt_alias = target.parent / "receipt-alias.json"
            os.link(receipt, receipt_alias)

            with self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "receipt is unsafe",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

            receipt_alias.unlink()
            real_hash = session_store_module._sha256_descriptor
            mutated = False

            def mutate_receipt_during_hash(descriptor):
                nonlocal mutated
                digest = real_hash(descriptor)
                if not mutated:
                    mutated = True
                    receipt.write_bytes(b"{}\n")
                return digest

            with mock.patch.object(
                session_store_module,
                "_sha256_descriptor",
                side_effect=mutate_receipt_during_hash,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "receipt changed during verification",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)

    def test_live_managed_writer_blocks_existing_target_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            target = root / "managed" / "sessions.sqlite3"
            legacy_store = SessionStore(legacy)
            legacy_store.create("legacy", "dev", "bash", root)
            legacy_store.close()
            target_store = SessionStore(target)
            target_store.create("existing", "dev", "bash", root)
            try:
                with self.assertRaisesRegex(
                    SessionDatabaseMigrationError,
                    "legacy session database is still open",
                ):
                    migrate_legacy_database(destination=target, legacy_path=legacy)
                self.assertIsNone(target_store.get("td-does-not-exist"))
                self.assertEqual(len(target_store.list()), 1)
            finally:
                target_store.close()

    def test_row_content_mismatch_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute(
                "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO marker(value) VALUES ('source')")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            real_backup = session_store_module._sqlite_backup

            def corrupt_backup(source, destination):
                summary = real_backup(source, destination)
                copied = sqlite3.connect(destination)
                try:
                    copied.execute("UPDATE marker SET value = 'corrupt'")
                    copied.commit()
                finally:
                    copied.close()
                return summary

            with mock.patch(
                "termdeck.session_store._sqlite_backup",
                side_effect=corrupt_backup,
            ), self.assertRaisesRegex(
                SessionDatabaseMigrationError,
                "row/window/schema parity",
            ):
                migrate_legacy_database(destination=target, legacy_path=legacy)
            self.assertFalse(target.exists())

    def test_content_digest_is_independent_of_insertion_order(self):
        first = sqlite3.connect(":memory:")
        second = sqlite3.connect(":memory:")
        try:
            for connection, rows in (
                (first, [(1, "one"), (2, "two"), (3, "three")]),
                (second, [(3, "three"), (1, "one"), (2, "two")]),
            ):
                connection.execute("CREATE TABLE marker (id INTEGER, value TEXT)")
                connection.executemany("INSERT INTO marker VALUES (?, ?)", rows)
                connection.commit()
            first_summary = session_store_module._database_summary(first)
            second_summary = session_store_module._database_summary(second)
            self.assertEqual(first_summary, second_summary)
        finally:
            first.close()
            second.close()

    def test_subset_proof_is_bounded_and_rejects_changed_values(self):
        source = sqlite3.connect(":memory:")
        destination = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        try:
            for connection in (source, destination):
                connection.execute(
                    "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)"
                )
                connection.executemany(
                    "INSERT INTO marker VALUES (?, ?)",
                    [(1, "one"), (2, "two")],
                )
                connection.commit()
            with mock.patch.object(
                session_store_module,
                "_MIGRATION_COMPARE_ROW_LIMIT",
                2,
            ):
                self.assertTrue(
                    session_store_module._database_is_lossless_subset(
                        destination,
                        source,
                    )
                )
            destination.execute("UPDATE marker SET value = 'changed' WHERE id = 2")
            destination.commit()
            self.assertFalse(
                session_store_module._database_is_lossless_subset(
                    destination,
                    source,
                )
            )
            with mock.patch.object(
                session_store_module,
                "_MIGRATION_COMPARE_ROW_LIMIT",
                1,
            ):
                self.assertFalse(
                    session_store_module._database_is_lossless_subset(
                        destination,
                        source,
                    )
                )
        finally:
            source.close()
            destination.close()

    def test_hot_rollback_journal_recovers_only_on_private_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            script = """
import os
import sqlite3
import sys

database = sys.argv[1]
connection = sqlite3.connect(database)
connection.execute("PRAGMA journal_mode = DELETE")
connection.execute("PRAGMA page_size = 1024")
connection.execute("PRAGMA cache_size = 4")
connection.execute("PRAGMA cache_spill = ON")
connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
connection.executemany(
    "INSERT INTO marker(id, value) VALUES (?, ?)",
    [(index, "old-" + "x" * 2000) for index in range(80)],
)
connection.commit()
connection.execute("BEGIN IMMEDIATE")
connection.executemany(
    "UPDATE marker SET value = ? WHERE id = ?",
    [("new-" + "y" * 2000, index) for index in range(80)],
)
os._exit(0)
"""
            subprocess.run(
                [sys.executable, "-c", script, str(legacy)],
                check=True,
                timeout=10,
            )
            journal = legacy.with_name(f"{legacy.name}-journal")
            self.assertTrue(journal.is_file())
            source_before = {
                path.name: path.read_bytes()
                for path in legacy.parent.iterdir()
                if path.is_file()
            }
            target = root / "managed" / "sessions.sqlite3"

            with mock.patch(
                "termdeck.session_store.sqlite3.connect",
                side_effect=self._guard_sqlite_source(legacy),
            ):
                result = migrate_legacy_database(
                    destination=target,
                    legacy_path=legacy,
                )

            self.assertEqual(result.status, "migrated")
            migrated = sqlite3.connect(target)
            try:
                values = migrated.execute(
                    "SELECT value FROM marker ORDER BY id"
                ).fetchall()
            finally:
                migrated.close()
            self.assertEqual(len(values), 80)
            self.assertTrue(all(value.startswith("old-") for (value,) in values))
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in legacy.parent.iterdir()
                    if path.is_file()
                },
                source_before,
            )

    def test_verified_restart_cleans_crash_left_migration_hardlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir()
            connection = sqlite3.connect(legacy)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.commit()
            connection.close()
            target = root / "managed" / "sessions.sqlite3"
            first = migrate_legacy_database(destination=target, legacy_path=legacy)
            residue = target.parent / ".sessions.sqlite3.migration-crash"
            os.link(target, residue)

            repeated = migrate_legacy_database(destination=target, legacy_path=legacy)

            self.assertEqual(repeated.status, "verified")
            self.assertEqual(repeated.database_id, first.database_id)
            self.assertFalse(residue.exists())
            self.assertEqual(target.stat().st_nlink, 1)

    def test_file_registry_uses_responsive_sqlite_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                journal_mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
                synchronous = store.conn.execute("PRAGMA synchronous").fetchone()[0]
                busy_timeout = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]

                self.assertEqual(str(journal_mode).casefold(), "wal")
                self.assertEqual(int(synchronous), 2)  # SQLite FULL: committed metadata is durable.
                self.assertEqual(int(busy_timeout), 1000)
            finally:
                store.close()

    def test_create_update_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("api", "dev", "python -m http.server", tmp)
                self.assertTrue(record.tmux_name.startswith("termdeck_dev_api"))
                updated = store.update(record.id, status="running", last_pid=123)
                self.assertEqual(updated.status, "running")
                self.assertEqual(updated.last_pid, 123)
                rows = store.list()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].id, record.id)
                self.assertEqual(store.tags(), ["dev"])
                self.assertIsNone(rows[0].recovery_command)
                self.assertIsNone(rows[0].recovery_cwd)
            finally:
                store.close()

    def test_window_captain_mapping_is_persistent_and_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                captain = store.create(
                    "captain",
                    "ai",
                    "codex",
                    tmp,
                    window_id="win-a",
                    window_title="A",
                )
                store.set_window_captain("win-a", captain.id)
                self.assertEqual(store.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
                self.assertFalse(store.clear_window_captain("win-a", session_id="td-not-captain"))
                self.assertEqual(store.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
            finally:
                store.close()

            reopened = SessionStore(db)
            try:
                self.assertEqual(reopened.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
                self.assertTrue(reopened.clear_window_captain("win-a", session_id=captain.id))
                self.assertIsNone(reopened.captain_for_window("win-a"))
            finally:
                reopened.close()

    def test_window_captain_validates_membership_and_cleans_stale_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "ai", "codex", tmp, window_id="win-a")
                second = store.create("second", "ai", "codex", tmp, window_id="win-a")
                foreign = store.create("foreign", "ai", "codex", tmp, window_id="win-b")

                with self.assertRaises(KeyError):
                    store.set_window_captain("win-a", "td-missing")
                with self.assertRaises(ValueError):
                    store.set_window_captain("win-a", foreign.id)

                store.set_window_captain("win-a", first.id)
                store.update(first.id, window_id="win-b")
                self.assertIsNone(store.captain_for_window("win-a"))
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM window_captains WHERE session_id = ?",
                        (first.id,),
                    ).fetchone()[0],
                    0,
                )

                store.set_window_captain("win-a", second.id)
                store.delete(second.id)
                self.assertIsNone(store.captain_for_window("win-a"))

                store.conn.execute(
                    "INSERT INTO window_captains (window_id, session_id) VALUES (?, ?)",
                    ("win-stale", "td-gone"),
                )
                store.conn.commit()
                self.assertIsNone(store.captain_for_window("win-stale"))
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM window_captains WHERE window_id = 'win-stale'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                store.close()

    def test_recovery_metadata_updates_are_batched_without_reordering_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "terminal", "/bin/bash", tmp)
                second = store.create("second", "terminal", "/bin/bash", tmp)
                second = store.update(
                    second.id,
                    recovery_command="codex --search",
                    recovery_cwd=str(Path(tmp) / "old"),
                    last_opened_at=second.last_opened_at,
                )
                first_opened_at = first.last_opened_at
                second_opened_at = second.last_opened_at

                recovery_cwd = str(Path(tmp) / "repo")
                count = store.update_recovery_metadata(
                    [
                        (first.id, "codex --yolo", recovery_cwd),
                        (second.id, None, str(Path(tmp) / "ignored")),
                    ]
                )

                self.assertEqual(count, 2)
                self.assertEqual(store.get(first.id).recovery_command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).recovery_cwd, recovery_cwd)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_cwd)  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).last_opened_at, first_opened_at)  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).last_opened_at, second_opened_at)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_exact_codex_uuid_is_not_downgraded_by_launcher_only_observation(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        exact = f"codex --yolo resume {session_id}"
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("agent", "ai", "/bin/bash", tmp)
                store.update(
                    record.id,
                    recovery_command=exact,
                    recovery_cwd=str(Path(tmp) / "exact"),
                )

                weaker_count = store.update_recovery_metadata(
                    [(record.id, "codex --yolo", str(Path(tmp) / "weaker"))]
                )
                preserved = store.get(record.id)

                self.assertEqual(weaker_count, 0)
                self.assertEqual(preserved.recovery_command, exact)  # type: ignore[union-attr]
                self.assertEqual(preserved.recovery_cwd, str(Path(tmp) / "exact"))  # type: ignore[union-attr]

                clear_count = store.update_recovery_metadata([(record.id, None, None)])
                cleared = store.get(record.id)
                self.assertEqual(clear_count, 1)
                self.assertIsNone(cleared.recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(cleared.recovery_cwd)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_runtime_reconciliation_updates_are_batched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "ai", "codex", tmp)
                second = store.create("second", "ai", "claude", tmp)

                count = store.update_runtime_states(
                    [
                        (first.id, "running", True, 101),
                        (second.id, "paused", False, 202),
                    ]
                )

                self.assertEqual(count, 2)
                refreshed_first = store.get(first.id)
                refreshed_second = store.get(second.id)
                self.assertEqual((refreshed_first.status, refreshed_first.visible, refreshed_first.last_pid), ("running", True, 101))
                self.assertEqual((refreshed_second.status, refreshed_second.visible, refreshed_second.last_pid), ("paused", False, 202))
            finally:
                store.close()

    def test_reopen_clears_server_local_pane_ids_only_for_managed_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                managed = store.create("managed", "ai", "codex", tmp)
                adopted = store.create("adopted", "external", "bash", tmp)
                store.conn.execute(
                    "UPDATE sessions SET pane_target = ? WHERE id = ?",
                    ("%161", managed.id),
                )
                store.update(adopted.id, external=True, pane_target="%9")
                store.conn.commit()
            finally:
                store.close()

            reopened = SessionStore(db)
            try:
                self.assertIsNone(reopened.get(managed.id).pane_target)  # type: ignore[union-attr]
                self.assertEqual(reopened.get(adopted.id).pane_target, "%9")  # type: ignore[union-attr]
                persisted = dict(
                    reopened.conn.execute(
                        "SELECT id, pane_target FROM sessions WHERE id IN (?, ?)",
                        (managed.id, adopted.id),
                    ).fetchall()
                )
                self.assertIsNone(persisted[managed.id])
                self.assertEqual(persisted[adopted.id], "%9")

                # A stale writer in the same server generation cannot appoint a
                # durable managed pane ID through either the read or update path.
                reopened.conn.execute(
                    "UPDATE sessions SET pane_target = ? WHERE id = ?",
                    ("%177", managed.id),
                )
                reopened.conn.commit()
                self.assertIsNone(reopened.get(managed.id).pane_target)  # type: ignore[union-attr]
                repaired = reopened.update(managed.id, pane_target="%178")
                self.assertIsNone(repaired.pane_target)
                raw_target = reopened.conn.execute(
                    "SELECT pane_target FROM sessions WHERE id = ?",
                    (managed.id,),
                ).fetchone()[0]
                self.assertIsNone(raw_target)
            finally:
                reopened.close()

    def test_window_restore_groups_visible_non_stopped_sessions_in_tab_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                second = store.create("second", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=1)
                first = store.create("first", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=0)
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=2)
                stopped = store.create("stopped", "dev", "bash", tmp, window_id="win-b", window_title="B", tab_index=0)
                third = store.create("third", "dev", "bash", tmp, window_id="win-b", window_title="B", tab_index=1)

                store.update(hidden.id, visible=False)
                store.update(stopped.id, status="stopped")

                groups = store.windows_with_visible_sessions()
                self.assertEqual(len(groups), 2)
                self.assertEqual(groups[0][0], "win-a")
                self.assertEqual([record.id for record in groups[0][2]], [first.id, second.id])
                self.assertEqual(groups[1][0], "win-b")
                self.assertEqual([record.id for record in groups[1][2]], [third.id])
            finally:
                store.close()

    def test_restore_candidates_prefers_visible_running_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                visible = store.create("visible", "dev", "bash", tmp, visible=True)
                hidden = store.create("hidden", "dev", "bash", tmp, visible=False)
                stopped = store.create("stopped", "dev", "bash", tmp, visible=True)
                store.update(stopped.id, status="stopped")

                restored = store.restore_candidates(limit=10)
                self.assertEqual([record.id for record in restored], [visible.id])

                store.update(visible.id, visible=False)
                fallback = store.restore_candidates(limit=10)
                self.assertEqual(fallback, [])
            finally:
                store.close()

    def test_each_terminal_gets_a_unique_private_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                root / "sessions.sqlite3",
                auth_root=root / "terminal-auth",
                account_source=root / "empty-source",
            )
            try:
                first = store.create("first", "ai", "bash", tmp)
                second = store.create("second", "ai", "bash", tmp)

                self.assertNotEqual(first.auth_scope_id, second.auth_scope_id)
                self.assertNotEqual(first.codex_home, second.codex_home)
                for record in (first, second):
                    home = Path(record.codex_home)
                    self.assertTrue(home.is_dir())
                    self.assertTrue((home / "bin" / "codex-auth").is_file())
                    self.assertFalse((home / "auth.json").exists())
            finally:
                store.close()

    def test_create_persists_proven_canonical_default_without_restart_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            accounts = source / "accounts"
            accounts.mkdir(parents=True)
            for name in ("alpha", "beta"):
                (accounts / f"{name}.json").write_text(
                    json.dumps(
                        {
                            "auth_mode": "chatgpt",
                            "tokens": {"refresh_token": f"{name}-refresh"},
                        }
                    ),
                    encoding="utf-8",
                )
            (source / "current").write_text("beta\n", encoding="utf-8")
            (source / "auth.json").symlink_to(Path("accounts") / "beta.json")
            store = SessionStore(
                root / "sessions.sqlite3",
                auth_root=root / "terminal-auth",
                account_source=source,
            )
            try:
                created = store.create("agent", "ai", "bash", tmp)
                persisted = store.get(created.id)

                self.assertEqual(created.selected_account, "beta")
                self.assertFalse(created.auth_restart_required)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.selected_account, "beta")  # type: ignore[union-attr]
                self.assertFalse(persisted.auth_restart_required)  # type: ignore[union-attr]
                home = Path(created.codex_home)
                self.assertTrue((home / "accounts").is_symlink())
                self.assertTrue((home / "auth.json").is_symlink())
                self.assertEqual(
                    (home / "auth.json").resolve(),
                    accounts / "beta.json",
                )
            finally:
                store.close()

    def test_create_uses_default_account_source_for_inherited_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_home = root / "host-home"
            source = host_home / ".codex"
            accounts = source / "accounts"
            accounts.mkdir(parents=True)
            for name in ("alpha", "beta"):
                (accounts / f"{name}.json").write_text(
                    json.dumps(
                        {
                            "auth_mode": "chatgpt",
                            "tokens": {"refresh_token": f"{name}-refresh"},
                        }
                    ),
                    encoding="utf-8",
                )
            (source / "current").write_text("beta\n", encoding="utf-8")
            (source / "auth.json").symlink_to(Path("accounts") / "beta.json")

            with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
                "termdeck.codex_auth.Path.home",
                return_value=host_home,
            ):
                os.environ.pop("TERMDECK_CODEX_ACCOUNT_SOURCE", None)
                store = SessionStore(
                    root / "sessions.sqlite3",
                    auth_root=root / "terminal-auth",
                )
                try:
                    created = store.create("agent", "ai", "bash", tmp)
                    persisted = store.get(created.id)

                    self.assertEqual(store.account_source, source)
                    self.assertEqual(created.selected_account, "beta")
                    self.assertFalse(created.auth_restart_required)
                    self.assertIsNotNone(persisted)
                    self.assertEqual(persisted.selected_account, "beta")  # type: ignore[union-attr]
                    self.assertFalse(persisted.auth_restart_required)  # type: ignore[union-attr]
                finally:
                    store.close()

    def test_direct_upsert_assigns_and_preserves_immutable_auth_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                root / "sessions.sqlite3",
                auth_root=root / "terminal-auth",
                account_source=root / "empty-source",
            )
            try:
                imported = SessionRecord(
                    id="td-adopted",
                    name="adopted",
                    tag="external",
                    command="bash",
                    cwd=tmp,
                    tmux_name="external-shell",
                    created_at="now",
                    last_opened_at="now",
                    external=True,
                )
                store.upsert(imported)
                saved = store.get(imported.id)
                self.assertIsNotNone(saved)
                self.assertTrue(saved.auth_scope_id)  # type: ignore[union-attr]
                self.assertTrue(Path(saved.codex_home).is_dir())  # type: ignore[arg-type,union-attr]

                replacement = SessionRecord(
                    **(
                        dict(saved.__dict__)  # type: ignore[union-attr]
                        | {
                            "auth_scope_id": "auth-forged",
                            "codex_home": str(root / "forged-home"),
                            "name": "updated",
                        }
                    )
                )
                store.upsert(replacement)
                preserved = store.get(imported.id)
                self.assertEqual(preserved.name, "updated")  # type: ignore[union-attr]
                self.assertEqual(preserved.auth_scope_id, saved.auth_scope_id)  # type: ignore[union-attr]
                self.assertEqual(preserved.codex_home, saved.codex_home)  # type: ignore[union-attr]
                with self.assertRaises(ValueError):
                    store.update(imported.id, codex_home=str(root / "other-home"))
            finally:
                store.close()

    def test_schema_migrates_database_from_first_mvp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    tmux_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_pid INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sessions
                    (id, name, tag, command, cwd, tmux_name, created_at, last_opened_at, status, last_pid)
                VALUES
                    ('td-old', 'old', 'default', 'bash', ?, 'termdeck_default_old', 'now', 'now', 'running', 42)
                """,
                (tmp,),
            )
            conn.commit()
            conn.close()

            store = SessionStore(
                db,
                account_source=Path(tmp) / "empty-source",
            )
            try:
                record = store.get("td-old")
                self.assertIsNotNone(record)
                self.assertEqual(record.window_id, "main")  # type: ignore[union-attr]
                self.assertEqual(record.window_title, "Main")  # type: ignore[union-attr]
                self.assertEqual(record.tab_index, 0)  # type: ignore[union-attr]
                self.assertTrue(record.visible)  # type: ignore[union-attr]
                self.assertIsNone(record.recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(record.recovery_cwd)  # type: ignore[union-attr]
                self.assertTrue(record.auth_scope_id)  # type: ignore[union-attr]
                self.assertTrue(Path(record.codex_home).is_dir())  # type: ignore[arg-type,union-attr]
                self.assertIsNone(record.selected_account)  # type: ignore[union-attr]
                self.assertFalse(record.auth_restart_required)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_schema_backfills_recovery_intent_from_legacy_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE sessions (
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
                    recovery_intent TEXT NOT NULL DEFAULT 'legacy'
                )
                """
            )
            rows = [
                ("running", "running", "legacy"),
                ("paused", "paused", "legacy"),
                ("missing", "missing", "legacy"),
                ("pending", "recovery-pending", "legacy"),
                ("stopped", "stopped", "legacy"),
                ("archived", "archived", "legacy"),
                ("new", "new", "legacy"),
                ("remembered", "remembered", "legacy"),
                ("start-error", "start-error", "legacy"),
                ("explicit-active", "stopped", "active"),
                ("explicit-inactive", "running", "inactive"),
            ]
            conn.executemany(
                """
                INSERT INTO sessions
                    (id, name, tag, command, cwd, tmux_name, created_at,
                     last_opened_at, status, last_pid, recovery_intent)
                VALUES (?, ?, 'default', 'bash', ?, ?, 'now', 'now', ?, NULL, ?)
                """,
                [
                    (session_id, session_id, tmp, f"termdeck_{session_id}", status, intent)
                    for session_id, status, intent in rows
                ],
            )
            conn.commit()
            conn.close()

            store = SessionStore(db)
            try:
                intents = {
                    record.id: record.recovery_intent
                    for record in store.list(include_archived=True)
                }
            finally:
                store.close()

            self.assertEqual(
                intents,
                {
                    "running": "active",
                    "paused": "active",
                    "missing": "active",
                    "pending": "active",
                    "stopped": "inactive",
                    "archived": "inactive",
                    "new": "inactive",
                    "remembered": "inactive",
                    "start-error": "legacy",
                    "explicit-active": "active",
                    "explicit-inactive": "inactive",
                },
            )


if __name__ == "__main__":
    unittest.main()


def test_explicit_termdeck_state_root_binds_database_and_auth_defaults() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        managed_root = Path(tmp) / "managed" / "t1000-managed"
        state_root = Path(tmp) / "canonical" / "termdeck-state"
        with mock.patch.dict(
            os.environ,
            {
                "T1000_MANAGED_ROOT": str(managed_root),
                "TERMDECK_STATE_ROOT": str(state_root),
            },
            clear=False,
        ):
            assert default_db_path() == state_root / "sessions.sqlite3"
            assert default_auth_root() == state_root / "terminal-auth"


def test_relative_termdeck_state_root_is_rejected() -> None:
    with mock.patch.dict(
        os.environ,
        {"TERMDECK_STATE_ROOT": "relative/termdeck-state"},
        clear=False,
    ):
        with unittest.TestCase().assertRaisesRegex(
            ValueError, "TERMDECK_STATE_ROOT must be an absolute path"
        ):
            default_db_path()
