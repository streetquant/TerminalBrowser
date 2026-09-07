from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

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
    assert result["protected_process_kill_evidence"]["allowlisted_units_only"] is True
    assert result["protected_process_kill_evidence"]["disappeared_processes_observed"] == result["disappeared"]
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

def test_service_units_are_published_from_staged_transaction(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx = paths.evidence / "tx-1"
    staged = tx / "staged-units"
    staged.mkdir(parents=True)
    for unit in activation.SERVICES:
        (staged / unit).write_text(f"[Unit]\nDescription={unit}\n", encoding="utf-8")

    activation._publish_units(paths, tx)

    for unit in activation.SERVICES:
        assert (paths.unit_dir / unit).read_text(encoding="utf-8") == (
            f"[Unit]\nDescription={unit}\n"
        )


def test_custom_watchdog_command_and_write_scope_are_complete(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tx = paths.evidence / "tx-1"
    unit = activation._watchdog_unit(paths, tx)

    for flag, value in (
        ("--transaction", tx),
        ("--target", paths.target),
        ("--evidence", paths.evidence),
        ("--runtime", paths.runtime),
        ("--database", paths.database),
        ("--managed-root", paths.managed_root),
        ("--managed-bin", paths.managed_bin),
        ("--unit-dir", paths.unit_dir),
    ):
        assert f"{flag} {value}" in unit
    for path in (paths.managed_root, paths.managed_bin):
        assert str(path) in unit
    assert "ProtectSystem=strict" in unit


def test_package_effect_before_receipt_ack_is_rolled_back_by_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    old_release = "a" * 64
    new_release = "b" * 64
    activation._write(
        paths.managed_root / "state.json",
        {"current_release_id": old_release},
    )
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt["status"] = "artifacts-staged"
    receipt["package"] = {
        "wheel": "qualified.whl",
        "manifest": "qualified.release.json",
        "release_id": new_release,
    }
    activation._write(tx / "receipt.json", receipt)

    class FakeThread:
        instances: list["FakeThread"] = []

        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False
            self.joined = False
            self.join_calls = 0
            self.instances.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.joined

        def join(self):
            self.join_calls += 1
            self.joined = True

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(activation.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda paths, tx, label: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda paths, tx: {})
    monkeypatch.setattr(activation, "_arm", lambda paths, tx: None)
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *args, check=True: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(activation, "_publish_units", lambda paths, tx: None)
    monkeypatch.setattr(activation, "_start_web", lambda: None)
    monkeypatch.setattr(activation, "_stop_web", lambda: None)
    monkeypatch.setattr(activation, "_disarm", lambda: None)
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda paths, tx, before: {
            "integrity": "ok",
            "lost_session_ids": [],
            "preserved_session_ids": 0,
        },
    )
    rollback_targets: list[object] = []

    def effect_then_crash(paths, package):
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": new_release},
        )
        raise RuntimeError("crash after package effect before receipt ACK")

    def restore_package(paths, release):
        rollback_targets.append(release)
        activation._write(
            paths.managed_root / "state.json",
            {"current_release_id": old_release},
        )

    monkeypatch.setattr(activation, "_install", effect_then_crash)
    monkeypatch.setattr(activation, "_rollback_package", restore_package)

    persist_statuses: list[str] = []
    real_persist = activation._persist

    def tracked_persist(paths, tx, receipt, status=None):
        if status is not None:
            persist_statuses.append(status)
            if status == "failed":
                assert FakeThread.instances[0].joined is True
        return real_persist(paths, tx, receipt, status)

    monkeypatch.setattr(activation, "_persist", tracked_persist)

    with pytest.raises(RuntimeError, match="crash after package effect"):
        activation.execute(paths, tx)

    final = activation._read(tx / "receipt.json")
    assert rollback_targets == [old_release]
    assert activation._read(paths.managed_root / "state.json")["current_release_id"] == old_release
    assert final["status"] == "rolled-back"
    assert final["package_rollback_required"] is True
    assert final["package_current_release_id"] == new_release
    assert "package-installed" not in persist_statuses
    assert FakeThread.instances[0].join_calls == 1

def test_execute_publishes_units_before_stopping_web_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.target.mkdir()
    paths.managed_root.mkdir()
    old_release = "a" * 64
    new_release = "b" * 64
    activation._write(paths.managed_root / "state.json", {"current_release_id": old_release})
    tx, receipt = activation.new_transaction(
        paths,
        source={"commit": "c" * 40, "tree": "d" * 40, "branch": "candidate", "origin_main": "c" * 40},
    )
    receipt["status"] = "artifacts-staged"
    receipt["package"] = {
        "wheel": "qualified.whl",
        "manifest": "qualified.release.json",
        "release_id": new_release,
    }
    activation._write(tx / "receipt.json", receipt)

    monkeypatch.setenv("TERMDECK_ACTIVATION_APPROVED", "1")
    monkeypatch.setattr(
        activation,
        "snapshot_database",
        lambda paths, tx, label: {"session_ids": [], "session_count": 0, "integrity": "ok"},
    )
    monkeypatch.setattr(activation, "process_snapshot", lambda: [])
    monkeypatch.setattr(activation, "_sources", lambda paths, tx: {})
    monkeypatch.setattr(activation, "_arm", lambda paths, tx: None)
    monkeypatch.setattr(
        activation,
        "_systemd",
        lambda *args, check=True: SimpleNamespace(returncode=0),
    )
    events: list[str] = []
    monkeypatch.setattr(
        activation,
        "_publish_units",
        lambda paths, tx: events.append("publish-units"),
    )
    monkeypatch.setattr(
        activation,
        "_install",
        lambda paths, package: (
            activation._write(
                paths.managed_root / "state.json",
                {"current_release_id": new_release},
            )
            or {"ok": True, "release_id": new_release}
        ),
    )
    monkeypatch.setattr(activation, "_stop_web", lambda: events.append("stop-web"))
    monkeypatch.setattr(activation, "_start_web", lambda: events.append("start-web"))
    monkeypatch.setattr(activation, "_health", lambda: {"backend": "ok"})
    monkeypatch.setattr(
        activation,
        "verify_database",
        lambda paths, tx, before: {"integrity": "ok", "lost_session_ids": []},
    )
    monkeypatch.setattr(
        activation,
        "process_observation",
        lambda before: {"disappeared": [], "natural_exit_allowed": True},
    )
    monkeypatch.setattr(activation, "_disarm", lambda: None)

    result = activation.execute(paths, tx)

    assert result["action"] == "activated"
    assert events.index("publish-units") < events.index("stop-web")
    final = activation._read(tx / "receipt.json")
    assert final["status"] == "activated"
    assert final["service_units_published"] is True

