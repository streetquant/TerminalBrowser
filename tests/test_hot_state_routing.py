from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import termdeck.cli as cli
import termdeck.session_store as session_store
import termdeck.storage_fabric as storage_fabric
from termdeck.session_store import SessionStore
from termdeck.storage_fabric import StorageFabricError


def _leaf_commands() -> tuple[tuple[str, object, bool], ...]:
    commands: list[tuple[str, object, bool]] = []

    def visit(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> None:
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparsers:
            commands.append(
                (
                    " ".join(prefix),
                    parser.get_default("func"),
                    any(action.dest == "db" for action in parser._actions),
                )
            )
            return
        for action in subparsers:
            for name, child in action.choices.items():
                visit(child, (*prefix, name))

    visit(cli.build_parser())
    return tuple(commands)


LEAF_COMMANDS = _leaf_commands()
DATABASE_COMMANDS = tuple(
    (name, function)
    for name, function, uses_session_db in LEAF_COMMANDS
    if uses_session_db
)
NON_GUI_DATABASE_COMMANDS = tuple(
    (name, function)
    for name, function in DATABASE_COMMANDS
    if function is not cli.cmd_gui
)
STATELESS_COMMANDS = tuple(
    (name, function)
    for name, function, uses_session_db in LEAF_COMMANDS
    if not uses_session_db
)


@pytest.mark.parametrize(
    ("command_name", "command_function"),
    DATABASE_COMMANDS,
    ids=lambda value: str(value).replace(" ", "-"),
)
def test_every_managed_database_command_attests_and_binds_the_default(
    command_name: str,
    command_function: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    database = managed_root.parent / "termdeck" / "sessions.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    observed: list[dict[str, object]] = []

    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setattr(
        cli,
        "require_hot_storage",
        lambda **kwargs: observed.append(kwargs),
    )
    args = argparse.Namespace(
        db=None,
        func=command_function,
        command=command_name.split()[0],
    )

    cli._prepare_managed_session_database(args)

    assert args.db == database
    assert observed == [
        {
            "managed_root": managed_root,
            "db_path": database,
            "production": True,
        }
    ]


@pytest.mark.parametrize(
    ("command_name", "command_function"),
    NON_GUI_DATABASE_COMMANDS,
    ids=lambda value: str(value).replace(" ", "-"),
)
def test_every_non_gui_managed_database_command_defers_before_primary_migration(
    command_name: str,
    command_function: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    legacy = tmp_path / "home" / ".local" / "share" / "termdeck" / "sessions.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-registry-must-remain-read-only")

    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setattr(cli, "require_hot_storage", lambda **_kwargs: None)
    args = argparse.Namespace(
        db=None,
        func=command_function,
        command=command_name.split()[0],
    )

    with pytest.raises(cli.ManagedSessionDatabaseUnavailable, match="migration is pending"):
        cli._prepare_managed_session_database(args)

    assert args.db == managed_root.parent / "termdeck" / "sessions.sqlite3"
    assert legacy.read_bytes() == b"legacy-registry-must-remain-read-only"
    assert sorted(path.name for path in legacy.parent.iterdir()) == [legacy.name]


def test_managed_explicit_database_rejection_reports_the_attested_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    explicit = tmp_path / "rotational" / "sessions.sqlite3"
    observed: dict[str, object] = {}

    def reject(**kwargs: object) -> None:
        observed.update(kwargs)
        raise StorageFabricError("database_root: backing device is rotational")

    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setattr(cli, "require_hot_storage", reject)
    args = argparse.Namespace(db=explicit, func=cli.cmd_sessions, command="sessions")

    with pytest.raises(StorageFabricError, match="database_root"):
        cli._prepare_managed_session_database(args)

    assert observed["db_path"] == explicit
    assert args.db == explicit


def test_unmanaged_development_cli_retains_explicit_database_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "development.sqlite3"
    monkeypatch.delenv("T1000_MANAGED_ROOT", raising=False)
    monkeypatch.setattr(
        cli,
        "require_hot_storage",
        lambda **_kwargs: pytest.fail("development CLI must not require production storage"),
    )
    args = argparse.Namespace(db=explicit, func=cli.cmd_sessions, command="sessions")

    cli._prepare_managed_session_database(args)

    assert args.db == explicit


@pytest.mark.parametrize(
    ("command_name", "command_function"),
    STATELESS_COMMANDS,
    ids=lambda value: str(value).replace(" ", "-"),
)
def test_every_stateless_managed_command_bypasses_database_cutover(
    command_name: str,
    command_function: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setattr(
        cli,
        "require_hot_storage",
        lambda **_kwargs: pytest.fail("stateless command entered database admission"),
    )
    args = argparse.Namespace(
        db=None,
        func=command_function,
        command=command_name.split()[0],
        _uses_session_db=False,
    )

    cli._prepare_managed_session_database(args)

    assert args.db is None


def test_managed_main_returns_configuration_error_without_opening_legacy_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    legacy = tmp_path / "home" / ".local" / "share" / "termdeck" / "sessions.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-registry")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setattr(cli, "require_hot_storage", lambda **_kwargs: None)

    result = cli.main(["sessions"])

    assert result == 78
    assert "migration is pending" in capsys.readouterr().err
    assert legacy.read_bytes() == b"legacy-registry"
    assert sorted(path.name for path in legacy.parent.iterdir()) == [legacy.name]


def test_managed_main_serializes_post_admission_database_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject(_args: argparse.Namespace) -> int:
        raise StorageFabricError("database_root: identity changed before SQLite open")

    args = argparse.Namespace(func=reject)
    parser = argparse.ArgumentParser()
    monkeypatch.setattr(parser, "parse_args", lambda _argv: args)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(cli, "_prepare_managed_session_database", lambda _args: None)

    result = cli.main(["sessions"])

    assert result == 78
    assert "identity changed before SQLite open" in capsys.readouterr().err


def _admit_database_parent(database: Path) -> storage_fabric._ProductionDatabaseBinding:
    parent = database.parent
    descriptor = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    metadata = os.fstat(descriptor)
    device = f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
    pinned = storage_fabric._PinnedDirectory(
        path=parent,
        descriptor=descriptor,
        device=device,
        identity=(metadata.st_dev, metadata.st_ino),
    )
    return storage_fabric._ProductionDatabaseBinding(
        path=database,
        expected_device=device,
        parent=pinned,
    )


def test_database_parent_swap_after_admission_fails_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "termdeck"
    parent.mkdir(mode=0o700)
    database = parent / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    original = tmp_path / "attested-termdeck"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    parent.rename(original)
    parent.symlink_to(attacker, target_is_directory=True)

    try:
        with pytest.raises(StorageFabricError, match="identity changed|symlink"):
            SessionStore(database)
    finally:
        admitted.close()

    assert list(attacker.iterdir()) == []
    assert list(original.iterdir()) == []


def test_database_swap_during_sqlite_open_never_writes_attacker_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "termdeck"
    parent.mkdir(mode=0o700)
    database = parent / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    original = tmp_path / "attested-termdeck"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    connect = session_store.sqlite3.connect
    swapped = False

    def swap_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped and str(args[0]).startswith("/proc/self/fd/"):
            parent.rename(original)
            parent.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return connect(*args, **kwargs)

    monkeypatch.setattr(session_store.sqlite3, "connect", swap_then_connect)
    try:
        with pytest.raises(StorageFabricError, match="identity changed|symlink"):
            SessionStore(database)
    finally:
        admitted.close()

    assert swapped
    assert list(attacker.iterdir()) == []
    assert (original / database.name).is_file()
    assert not (attacker / database.name).exists()


def test_gtk_tmux_event_watcher_uses_enforced_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import termdeck.gtk_app as gtk_app

    expected_environment = {
        "TERMDECK_TMUX_RUNTIME": "/ssd/termdeck/tmux",
        "TMUX_TMPDIR": "/proc/123/fd/9",
    }
    observed: dict[str, object] = {}

    def environment(*, materialize: bool = False):
        observed["materialize"] = materialize
        return expected_environment

    def popen(command: list[str], **kwargs: object) -> object:
        observed["command"] = command
        observed["popen"] = kwargs
        return object()

    class FakeControlClient:
        def __init__(self, *, spawn_transport, **_kwargs: object) -> None:
            self.spawn_transport = spawn_transport

        def start(self) -> None:
            observed["transport"] = self.spawn_transport()

    monkeypatch.setattr(gtk_app, "tmux_subprocess_environment", environment)
    monkeypatch.setattr(gtk_app.subprocess, "Popen", popen)
    monkeypatch.setattr(gtk_app, "ControlModeClient", FakeControlClient)
    app = SimpleNamespace(
        _tmux_events_client=None,
        backend=SimpleNamespace(available=True, executable="/usr/bin/tmux"),
        _on_tmux_control_event=lambda _event: None,
    )

    gtk_app.TermDeckApplication._start_tmux_event_watcher(app)

    assert observed["materialize"] is True
    assert observed["command"] == ["/usr/bin/tmux", "-C", "attach-session"]
    assert observed["popen"]["env"] is expected_environment  # type: ignore[index]


def test_explicit_state_root_routes_managed_cli_to_the_canonical_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed" / "t1000-managed"
    state_root = tmp_path / "canonical" / "termdeck-state"
    database = state_root / "sessions.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    observed: list[dict[str, object]] = []

    monkeypatch.setenv("T1000_MANAGED_ROOT", str(managed_root))
    monkeypatch.setenv("TERMDECK_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        cli,
        "require_hot_storage",
        lambda **kwargs: observed.append(kwargs),
    )
    args = argparse.Namespace(db=None, func=cli.cmd_sessions, command="sessions")

    cli._prepare_managed_session_database(args)

    assert args.db == database
    assert observed == [
        {"managed_root": managed_root, "db_path": database, "production": True}
    ]


def _assert_live_sqlite_locks_and_child_read(database: Path) -> None:
    # A different process must not acquire either SQLite's SHM dead-man-switch
    # byte or its main-file shared-lock byte while our connections are alive.
    # Only attempt the ordinary child SQLite open once both locks are proven;
    # a failed regression must not truncate a live parent mapping.
    program = """
import fcntl, json, os, sqlite3, sys
database = sys.argv[1]
blocked = []
for path, offset in ((database + '-shm', 128), (database, 1073741826)):
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset, os.SEEK_SET)
            blocked.append(False)
        except BlockingIOError:
            blocked.append(True)
    finally:
        os.close(fd)
value = None
if all(blocked):
    connection = sqlite3.connect(database)
    value = connection.execute('SELECT value FROM lock_sentinel').fetchone()[0]
    connection.close()
print(json.dumps({'blocked': blocked, 'value': value}))
"""
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(database)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert json.loads(child.stdout) == {"blocked": [True, True], "value": 7}
    assert database.with_name(f"{database.name}-shm").stat().st_size >= 32768


def _initialize_lock_sentinel(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("CREATE TABLE lock_sentinel(value INTEGER)")
    connection.execute("INSERT INTO lock_sentinel VALUES(7)")
    connection.commit()


@pytest.mark.parametrize("production", [False, True])
def test_two_session_stores_retain_sqlite_locks_across_hardening_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: bool,
) -> None:
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    database = parent / "sessions.sqlite3"
    monkeypatch.setenv("TERMDECK_STATE_ROOT", str(parent))
    admitted = _admit_database_parent(database) if production else None
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    stores: list[SessionStore] = []
    try:
        stores.append(SessionStore(database, provision_auth_scopes=False))
        _initialize_lock_sentinel(stores[0].conn)
        _assert_live_sqlite_locks_and_child_read(database)
        stores.append(SessionStore(database, provision_auth_scopes=False))
        _assert_live_sqlite_locks_and_child_read(database)
        stores.pop(0).close()
        _assert_live_sqlite_locks_and_child_read(database)
        stores[0].conn.execute("UPDATE lock_sentinel SET value=7")
        stores[0].conn.commit()
        assert stores[0].conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for suffix in ("", "-wal", "-shm"):
            assert stat.S_IMODE(Path(f"{database}{suffix}").stat().st_mode) == 0o600
    finally:
        for store in stores:
            store.close()
        if admitted is not None:
            admitted.close()


@pytest.mark.parametrize("hardener", ["path", "migration-parent"])
def test_sqlite_metadata_hardening_keeps_existing_connection_locks(
    tmp_path: Path, hardener: str,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    connection = sqlite3.connect(database)
    _initialize_lock_sentinel(connection)
    admitted = _admit_database_parent(database)
    try:
        for suffix in ("", "-wal", "-shm"):
            os.chmod(f"{database}{suffix}", 0o644)
        if hardener == "path":
            session_store._harden_database_permissions(database)
        else:
            session_store._harden_database_at(admitted.parent, database.name)
        for suffix in ("", "-wal", "-shm"):
            assert stat.S_IMODE(Path(f"{database}{suffix}").stat().st_mode) == 0o600
        _assert_live_sqlite_locks_and_child_read(database)
    finally:
        connection.close()
        admitted.close()


def test_fresh_database_publication_closes_normal_fd_before_sqlite_can_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    real_link = os.link
    connection = None
    bound = None

    def publish_then_open(*args, **kwargs):
        nonlocal connection
        real_link(*args, **kwargs)
        identity = storage_fabric._directory_identity(database.stat())
        descriptors = []
        for entry in Path("/proc/self/fd").iterdir():
            descriptor = int(entry.name)
            try:
                metadata = os.fstat(descriptor)
            except OSError:
                continue
            if storage_fabric._directory_identity(metadata) == identity:
                descriptors.append(descriptor)
                assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_PATH
        assert descriptors, "the created inode must remain pinned at publication"
        # Deterministic scheduling barrier: SQLite has acquired its locks before
        # the creator returns from publication and closes any remaining pins.
        connection = sqlite3.connect(database)
        _initialize_lock_sentinel(connection)

    monkeypatch.setattr(storage_fabric.os, "link", publish_then_open)
    try:
        bound = storage_fabric.pin_production_database(database)
        assert bound is not None and connection is not None
        assert fcntl.fcntl(bound.descriptor, fcntl.F_GETFL) & os.O_PATH
        assert database.stat().st_nlink == 1
        assert not list(tmp_path.glob(".sessions.sqlite3.create-*"))
        _assert_live_sqlite_locks_and_child_read(database)
        bound.close()
        bound = None
        _assert_live_sqlite_locks_and_child_read(database)
    finally:
        if bound is not None:
            bound.close()
        if connection is not None:
            connection.close()
        admitted.close()


def test_fresh_database_publication_preserves_a_concurrent_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    real_link = os.link
    winner_identity = None

    def another_creator_wins(*args, **kwargs):
        nonlocal winner_identity
        database.write_bytes(b"concurrent creator")
        winner_identity = storage_fabric._directory_identity(database.stat())
        return real_link(*args, **kwargs)

    monkeypatch.setattr(storage_fabric.os, "link", another_creator_wins)
    bound = None
    try:
        bound = storage_fabric.pin_production_database(database)
        assert bound is not None and bound.identity == winner_identity
        assert database.read_bytes() == b"concurrent creator"
        assert not list(tmp_path.glob(".sessions.sqlite3.create-*"))
    finally:
        if bound is not None:
            bound.close()
        admitted.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
@pytest.mark.parametrize("hardener", ["path", "migration-parent"])
def test_sqlite_hardening_refuses_unsafe_sidecars_without_changing_target(
    tmp_path: Path, kind: str, hardener: str,
) -> None:
    target = tmp_path / "foreign"
    target.write_bytes(b"untouched")
    target.chmod(0o644)
    sidecar = tmp_path / "sessions.sqlite3-shm"
    if kind == "symlink":
        sidecar.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, sidecar)
    else:
        sidecar.mkdir(mode=0o755)
    admitted = _admit_database_parent(tmp_path / "sessions.sqlite3")
    try:
        with pytest.raises(session_store.SessionDatabaseMigrationError):
            if hardener == "path":
                session_store._harden_regular_file(sidecar, 0o600)
            else:
                session_store._harden_regular_at(admitted.parent, sidecar.name, 0o600)
        assert target.read_bytes() == b"untouched"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    finally:
        admitted.close()


def test_database_hardening_uses_pinned_inode_and_refuses_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    database.write_bytes(b"original")
    database.chmod(0o644)
    original = tmp_path / "original"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    real_chmod = os.chmod

    def replace_before_chmod(path, mode, **kwargs):
        assert str(path).startswith("/proc/self/fd/")
        database.rename(original)
        database.write_bytes(b"foreign")
        real_chmod(database, 0o644)
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(storage_fabric.os, "chmod", replace_before_chmod)
    try:
        with pytest.raises(StorageFabricError, match="identity changed"):
            storage_fabric.pin_production_database(database)
        assert database.read_bytes() == b"foreign"
        assert stat.S_IMODE(database.stat().st_mode) == 0o644
        assert original.read_bytes() == b"original"
        assert stat.S_IMODE(original.stat().st_mode) == 0o600
    finally:
        admitted.close()


def test_fresh_database_refuses_replaced_temporary_without_unlinking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    real_open = storage_fabric._open_sqlite_inode
    replacement = None

    def substitute_temporary(path, **kwargs):
        nonlocal replacement
        if str(path).startswith(".sessions.sqlite3.create-"):
            replacement = tmp_path / path
            replacement.rename(tmp_path / "retained-original")
            replacement.write_bytes(b"foreign temporary")
        return real_open(path, **kwargs)

    monkeypatch.setattr(storage_fabric, "_open_sqlite_inode", substitute_temporary)
    try:
        with pytest.raises(StorageFabricError, match="temporary was replaced"):
            storage_fabric.pin_production_database(database)
        assert not database.exists()
        assert replacement is not None
        assert replacement.read_bytes() == b"foreign temporary"
        assert (tmp_path / "retained-original").read_bytes() == b""
    finally:
        admitted.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "owner", "device"])
def test_database_pin_preserves_unsafe_inode_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    target.chmod(0o644)
    if kind == "symlink":
        database.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, database)
    else:
        target.rename(database)
        target = database
    admitted = _admit_database_parent(database)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", admitted)
    if kind == "device":
        admitted.expected_device = "-1:-1"
    elif kind == "owner":
        identity = storage_fabric._directory_identity(database.stat())
        real_fstat = os.fstat

        def foreign_owner(descriptor):
            metadata = real_fstat(descriptor)
            if storage_fabric._directory_identity(metadata) == identity:
                fields = list(metadata)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return metadata

        monkeypatch.setattr(storage_fabric.os, "fstat", foreign_owner)
    try:
        with pytest.raises(StorageFabricError):
            storage_fabric.pin_production_database(database)
        assert target.read_bytes() == b"untouched"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    finally:
        admitted.close()


def test_fresh_database_fstat_failure_closes_unpublished_normal_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    admitted = _admit_database_parent(database)
    real_fstat = os.fstat
    created_descriptor = None

    def fail_created_fstat(descriptor):
        nonlocal created_descriptor
        if ".sessions.sqlite3.create-" in os.readlink(f"/proc/self/fd/{descriptor}"):
            created_descriptor = descriptor
            raise OSError("injected creation metadata failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(storage_fabric.os, "fstat", fail_created_fstat)
    try:
        with pytest.raises(OSError, match="injected creation metadata failure"):
            storage_fabric._create_sqlite_inode(admitted.parent, database.name)
        assert created_descriptor is not None
        with pytest.raises(OSError):
            fcntl.fcntl(created_descriptor, fcntl.F_GETFD)
        assert not database.exists()
        leftovers = list(tmp_path.glob(".sessions.sqlite3.create-*"))
        assert len(leftovers) == 1  # identity was never established; preserve it
        assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
    finally:
        admitted.close()
