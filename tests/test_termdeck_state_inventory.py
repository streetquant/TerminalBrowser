from __future__ import annotations

import os
from pathlib import Path

import pytest

from termdeck.state_inventory import StateInventoryError, inventory_state


def _fixture(root: Path) -> Path:
    state = root / "state"
    auth = state / "terminal-auth" / "auth-0001" / "nested"
    auth.mkdir(parents=True)
    (state / "sessions.sqlite3").write_bytes(b"sessions")
    (state / "sessions.sqlite3-wal").write_bytes(b"")
    (state / "sessions.sqlite3-shm").write_bytes(b"shm")
    primary = auth / "history.sqlite3"
    primary.write_bytes(b"auth-primary")
    (auth / "history.sqlite3-wal").write_bytes(b"nonzero-wal")
    (auth / "history.sqlite3-shm").write_bytes(b"shm")
    (auth / "empty.sqlite3").write_bytes(b"empty-primary")
    (auth / "empty.sqlite3-wal").write_bytes(b"")
    (auth / "empty.sqlite3-shm").write_bytes(b"shm")
    (auth / "unpaired.sqlite3-wal").write_bytes(b"orphan")
    (auth / "notes.txt").write_bytes(b"not sqlite")
    external = root / "external"
    external.write_text("outside", encoding="utf-8")
    (auth / "external-link").symlink_to(external)
    return state


def _proc_holder_fixture(root: Path, state: Path) -> tuple[Path, Path]:
    proc = root / "proc"
    process = proc / "4242"
    fd_directory = process / "fd"
    fd_directory.mkdir(parents=True)
    (process / "status").write_text(
        f"Name:\tfixture-holder\nUid:\t{os.geteuid()}\t{os.geteuid()}\t{os.geteuid()}\t{os.geteuid()}\n",
        encoding="utf-8",
    )
    descriptor = fd_directory / "7"
    descriptor.symlink_to(state / "sessions.sqlite3")
    return proc, descriptor


def test_inventory_attests_sessions_cohort_and_terminal_auth_wals(tmp_path: Path) -> None:
    state = _fixture(tmp_path)

    report = inventory_state(state, check_holders=False)

    assert report["schema"] == "termdeck.state-inventory.v1"
    assert report["source_path"] == str(state)
    assert report["source_paths"] == [str(state), str(state / "terminal-auth")]
    database = report["sessions_database"]
    assert isinstance(database, dict)
    assert database["source_path"] == str(state / "sessions.sqlite3")
    assert database["source_paths"] == [
        str(state / "sessions.sqlite3"),
        str(state / "sessions.sqlite3-wal"),
        str(state / "sessions.sqlite3-shm"),
    ]
    components = database["components"]
    assert isinstance(components, dict)
    assert components["primary"]["sha256"]
    auth = report["terminal_auth"]
    assert isinstance(auth, dict)
    counts = auth["counts"]
    assert isinstance(counts, dict)
    assert counts["primary_count"] == 2
    assert counts["wal_count"] == 3
    assert counts["wal_nonzero_count"] == 2
    assert counts["shm_count"] == 2
    assert counts["orphan_sidecar_count"] == 1
    assert counts["symlinks"] == 1
    assert report["quiescence"]["quiescent"] is True
    with pytest.raises(StateInventoryError, match="cohorts are incomplete"):
        inventory_state(state, check_holders=False, require_complete_cohorts=True)


def test_inventory_fails_closed_on_holder_and_recovers_after_close(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    proc_root, descriptor = _proc_holder_fixture(tmp_path, state)
    report = inventory_state(
        state,
        proc_root=proc_root,
        holder_timeout_seconds=1.0,
        require_quiescent=False,
    )
    quiescence = report["quiescence"]
    assert isinstance(quiescence, dict)
    assert quiescence["quiescent"] is False
    holders = quiescence["holders"]
    assert isinstance(holders, list)
    assert holders == [
        {"pid": 4242, "name": "fixture-holder", "file_count": 1, "paths": ["../sessions.sqlite3"]}
    ]
    with pytest.raises(StateInventoryError, match="still held open"):
        inventory_state(state, proc_root=proc_root, require_quiescent=True)

    descriptor.unlink()
    after = inventory_state(state, proc_root=proc_root, require_quiescent=False)
    after_quiescence = after["quiescence"]
    assert isinstance(after_quiescence, dict)
    assert after_quiescence["proc_scan_complete"] is True
    assert after_quiescence["quiescent"] is True


def test_holder_scan_budget_is_bounded_and_fails_closed(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    proc = tmp_path / "proc"
    for pid in ("1001", "1002"):
        process = proc / pid
        (process / "fd").mkdir(parents=True)
        (process / "status").write_text(
            f"Name:\tfixture-{pid}\nUid:\t{os.geteuid()}\t{os.geteuid()}\t{os.geteuid()}\t{os.geteuid()}\n",
            encoding="utf-8",
        )
    (proc / "1001" / "fd" / "7").symlink_to(state / "sessions.sqlite3")
    (proc / "1001" / "fd" / "8").symlink_to(state / "sessions.sqlite3-shm")

    report = inventory_state(
        state,
        proc_root=proc,
        holder_max_descriptors=1,
        holder_timeout_seconds=1.0,
    )
    quiescence = report["quiescence"]
    assert isinstance(quiescence, dict)
    assert quiescence["proc_scan_complete"] is False
    assert quiescence["quiescent"] is False
    with pytest.raises(StateInventoryError, match="cannot prove"):
        inventory_state(
            state,
            proc_root=proc,
            holder_max_descriptors=1,
            holder_timeout_seconds=1.0,
            require_quiescent=True,
        )


def test_inventory_rejects_auth_root_outside_state_root(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    external = tmp_path / "other-auth"
    external.mkdir()

    with pytest.raises(StateInventoryError, match="inside state source root"):
        inventory_state(state, terminal_auth_root=external, check_holders=False)


def test_inventory_requires_absolute_source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(StateInventoryError, match="absolute path"):
        inventory_state(Path("state"), check_holders=False)
