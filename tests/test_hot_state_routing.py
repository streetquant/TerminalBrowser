from __future__ import annotations

import argparse
import os
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
