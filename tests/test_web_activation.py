from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from termdeck import web_activation as activation


def _paths(tmp_path: Path) -> activation.ActivationPaths:
    return activation.ActivationPaths(
        target=tmp_path / "source",
        evidence=tmp_path / "evidence",
        runtime=tmp_path / "runtime",
        database=tmp_path / "sessions.sqlite3",
        managed_root=tmp_path / "managed",
        managed_bin=tmp_path / "bin",
        unit_dir=tmp_path / "units",
    )


def test_transaction_pointer_and_receipt_exist_before_build(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )

    assert receipt["status"] == "prepared"
    assert receipt["mutation_started"] is False
    pointer = activation._read(paths.latest)
    assert pointer["transaction"] == tx.name
    assert activation._read(tx / "receipt.json")["status"] == "prepared"

    activation._persist(paths, tx, receipt, status="building-package")
    assert activation._read(tx / "receipt.json")["mutation_started"] is False
    activation._persist(paths, tx, receipt, status="mutation-start")
    assert activation._read(tx / "receipt.json")["mutation_started"] is True


@pytest.mark.parametrize("status", sorted(activation.MUTATION_PHASES))
def test_every_mutation_phase_is_a_rollback_candidate(tmp_path: Path, status: str) -> None:
    paths = _paths(tmp_path)
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    receipt["mutation_started"] = True
    receipt["status"] = status
    activation._write(tx / "receipt.json", receipt)

    loaded_tx, loaded = activation.load_transaction(paths, tx)
    assert loaded_tx == tx
    assert loaded["status"] == status


def test_live_backup_preserves_existing_session_ids_while_activity_changes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    connection = sqlite3.connect(paths.database)
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT)")
    connection.executemany("INSERT INTO sessions VALUES (?, ?)", [("keep-a", "running"), ("keep-b", "stopped")])
    connection.commit()
    connection.close()

    tx, _receipt = activation.new_transaction(
        paths,
        source={"commit": "a" * 40, "tree": "b" * 40, "branch": "candidate", "origin_main": "a" * 40},
    )
    before = activation.snapshot_database(paths, tx, "before")
    connection = sqlite3.connect(paths.database)
    connection.execute("UPDATE sessions SET status='natural-exit' WHERE id='keep-a'")
    connection.execute("INSERT INTO sessions VALUES ('new-c', 'running')")
    connection.commit()
    connection.close()

    result = activation.verify_database(paths, tx, before)
    assert result["integrity"] == "ok"
    assert result["preserved_session_ids"] == 2
    assert result["new_session_ids"] == 1
    assert result["lost_session_ids"] == []
    assert result["wal_shm_byte_identity_required"] is False


def test_process_observation_records_natural_exit_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        activation,
        "process_snapshot",
        lambda: [{"pid": 20, "start_ticks": 4, "comm": "tmux"}],
    )
    result = activation.process_observation(
        [
            {"pid": 10, "start_ticks": 3, "comm": "codex"},
            {"pid": 20, "start_ticks": 4, "comm": "tmux"},
        ]
    )
    assert result["natural_exit_allowed"] is True
    assert result["protected_processes_killed_by_activation"] is False
    assert result["disappeared"] == [{"comm": "codex", "pid": 10, "start_ticks": 3}]


def test_watchdog_is_armed_as_a_separate_user_unit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    unit = activation._watchdog_unit(paths, tmp_path / "evidence" / "tx-1")

    assert "termdeck-web-recovery.service" not in "\n".join(activation.SERVICES)
    assert "--watchdog --transaction" in unit
    assert str(tmp_path / "evidence" / "tx-1") in unit
    assert "Restart=no" in unit
    assert "CODEX_BULK_STORAGE_MASKED" not in unit
    assert "warp-drives-v1" not in unit
    assert "ssd-cell" not in unit

