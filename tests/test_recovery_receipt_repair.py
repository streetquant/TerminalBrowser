from __future__ import annotations

from dataclasses import replace
import json
import hashlib
from pathlib import Path

import pytest

from termdeck.recovery_coordinator import RecoveryCoordinator
from termdeck.recovery_receipt_repair import PLAN_SCHEMA, _read_plan, reenroll_saved_receipts
from termdeck.session_recipe import SessionRecipeError, parse_session_recipe, recipe_digest, receipt_reenrollment
from termdeck.session_store import SessionStore
from termdeck.startup_recovery import recover_saved_terminals


class Backend:
    def __init__(self):
        self.observations = {}
        self.live = {}
        self.created = []

    def reconciliation_snapshot(self, records):
        return {r.id: self.observations.get(r.id, {"state": "missing", "pid": None}) for r in records}

    def authoritative_live_pane_pid(self, record):
        return self.live.get(record.id)

    pane_pid = authoritative_live_pane_pid

    def session_exists(self, record):
        return record.id in self.live

    def create_or_resume(self, record):
        self.created.append(record)
        self.live[record.id] = 9200 + len(self.created)


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    yield value
    value.close()


def broken(store, tmp_path, *, digest="0" * 64, name="saved"):
    item = store.create(name, "work", "/usr/bin/bash --noprofile --norc -i", str(tmp_path))
    item = store.update(item.id, status="running")
    # Retained malformed legacy fixture; production uses only the native CAS.
    store.conn.execute("UPDATE sessions SET recovery_recipe_sha256 = ?, automatic_name = NULL WHERE id = ?",
                       (digest, item.id))
    store.conn.commit()
    return store.get(item.id)


def rows(store):
    return {r["id"]: dict(r) for r in store.conn.execute("SELECT * FROM sessions ORDER BY id")}


def tables(store):
    return {r[0]: [tuple(x) for x in store.conn.execute(f'SELECT * FROM "{r[0]}" ORDER BY rowid')]
            for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sessions'")}


def plan(store, records):
    return {"schema": PLAN_SCHEMA,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "entries": [{"id": r.id,
                         "row_sha256": store.session_row_sha256(rows(store)[r.id]),
                         "payload_sha256": recipe_digest(r.recovery_recipe),
                         "new_digest": recipe_digest(r.recovery_recipe)} for r in records]}


def repair(store, backend, tmp_path, selection):
    return reenroll_saved_receipts(store, backend, plan=selection,
        runtime_root=tmp_path / "runtime", quarantine_path=tmp_path / "quarantine/preimages.json")


@pytest.mark.parametrize("digest", [None, "", "0" * 64])
def test_explicit_reenrollment_preserves_every_other_raw_field_and_table(store, tmp_path, digest):
    selected = broken(store, tmp_path, digest=digest)
    other = broken(store, tmp_path, name="unselected")
    before, before_tables = rows(store), tables(store)
    result = repair(store, Backend(), tmp_path, plan(store, [selected]))
    after = rows(store)
    expected = dict(before[selected.id], recovery_recipe_sha256=recipe_digest(selected.recovery_recipe))
    assert after == {selected.id: expected, other.id: before[other.id]}
    assert tables(store) == before_tables
    assert result["terminal_actions"] == 0 and result["old_receipts_accepted"] is False
    old = json.loads((tmp_path / "quarantine/preimages.json").read_text())
    assert old["rows"] == {selected.id: before[selected.id]}
    assert (tmp_path / "quarantine/preimages.json").stat().st_mode & 0o777 == 0o600
    assert parse_session_recipe(after[selected.id]["recovery_recipe"], after[selected.id]["recovery_recipe_sha256"])


def test_changed_full_raw_row_cas_aborts_whole_batch(store, tmp_path):
    one, two = broken(store, tmp_path), broken(store, tmp_path, name="two")
    selection = plan(store, [one, two])
    store.conn.execute("UPDATE sessions SET tag = ? WHERE id = ?", ("concurrent-user-change", two.id))
    store.conn.commit()
    before = rows(store)
    with pytest.raises(ValueError, match="preimage changed"):
        store.reenroll_recovery_receipts({e["id"]: e["row_sha256"] for e in selection["entries"]},
                                       assert_quiescent=lambda records: None)
    assert rows(store) == before


def test_distinct_command_or_missing_environment_is_never_inferred(store, tmp_path):
    item = broken(store, tmp_path)
    with pytest.raises(SessionRecipeError, match="disagrees"):
        receipt_reenrollment(replace(item, recovery_command="different saved command"))
    payload = json.loads(item.recovery_recipe)
    del payload["environment"]
    with pytest.raises(SessionRecipeError, match="fields are invalid"):
        receipt_reenrollment(replace(item, recovery_recipe=json.dumps(payload)))
    valid = replace(item, recovery_recipe_sha256=recipe_digest(item.recovery_recipe))
    with pytest.raises(SessionRecipeError, match="already valid"):
        receipt_reenrollment(valid)


@pytest.mark.parametrize("state", ["live", "identity-mismatch", "unobservable"])
def test_live_or_uncertain_panes_block_entire_batch_and_keep_preimages(store, tmp_path, state):
    one, two = broken(store, tmp_path), broken(store, tmp_path, name="two")
    before, before_tables = rows(store), tables(store)
    backend = Backend()
    backend.observations[two.id] = {"state": state, "pid": 9123}
    with pytest.raises(ValueError, match="live or unobservable"):
        repair(store, backend, tmp_path, plan(store, [one, two]))
    assert rows(store) == before and tables(store) == before_tables
    assert json.loads((tmp_path / "quarantine/preimages.json").read_text())["rows"] == before
    assert backend.created == []


def test_expired_claim_with_live_or_unknown_owner_still_blocks_repair(store, tmp_path):
    item = broken(store, tmp_path)
    coordinator = RecoveryCoordinator(Backend(), store)
    try:
        coordinator._claim_table(store.conn)
        store.conn.execute("INSERT INTO recovery_claims VALUES (?, ?, ?, ?, ?)",
            (coordinator._single_flight_keys(item)[0], item.id, coordinator.owner_id, "old", 0))
        store.conn.commit()
        before = rows(store)
        with pytest.raises(RuntimeError, match="active or unverifiable owner"):
            repair(store, Backend(), tmp_path, plan(store, [item]))
        assert rows(store) == before
    finally:
        coordinator.shutdown()


def test_gtk_target_lock_prevents_metadata_writer_and_releases_after_timeout(store, tmp_path):
    item = broken(store, tmp_path)
    first = RecoveryCoordinator(Backend(), store)
    second = RecoveryCoordinator(Backend(), store)
    try:
        with first.metadata_writer_guard([item]):
            with pytest.raises(RuntimeError, match="writer is busy"):
                with second.metadata_writer_guard([item], timeout=0.02):
                    pytest.fail("overlapping writer entered")
        with second.metadata_writer_guard([item], timeout=0.02):
            pass
    finally:
        first.shutdown()
        second.shutdown()


def test_explicit_repair_enables_exact_saved_launch_without_auth_provision(store, tmp_path):
    item = broken(store, tmp_path)
    backend = Backend()
    repair(store, backend, tmp_path, plan(store, [item]))
    result = recover_saved_terminals(store, backend, runtime_root=tmp_path / "runtime",
                                    readiness_timeout=0.5)
    assert result["counts"] == {"ready": 1}
    launched = backend.created[0]
    assert (launched.command, launched.cwd, launched.recovery_recipe) == (
        item.command, item.cwd, item.recovery_recipe)
    assert launched.codex_home is None and launched.auth_scope_id is None
    assert not store.auth_root.exists()


def test_inverse_restores_exact_invalid_preimages_and_rejects_later_changes(store, tmp_path):
    item = broken(store, tmp_path)
    before, before_tables = rows(store), tables(store)
    selection = plan(store, [item])
    repair(store, Backend(), tmp_path, selection)
    assert store.rollback_receipt_reenrollment(before, assert_quiescent=lambda records: None) == {item.id}
    assert rows(store) == before and tables(store) == before_tables
    store.reenroll_recovery_receipts({item.id: selection["entries"][0]["row_sha256"]},
                                   assert_quiescent=lambda records: None)
    store.update(item.id, tag="new-user-edit")
    edited = rows(store)
    with pytest.raises(ValueError, match="enrolled row changed"):
        store.rollback_receipt_reenrollment(before, assert_quiescent=lambda records: None)
    assert rows(store) == edited


def test_read_plan_is_pinned_to_exact_private_physical_payload(store, tmp_path):
    item = broken(store, tmp_path)
    value = plan(store, [item])
    path = tmp_path / "plan.json"
    encoded = json.dumps(value).encode()
    path.write_bytes(encoded)
    path.chmod(0o400)
    digest = hashlib.sha256(encoded).hexdigest()
    assert _read_plan(path, digest) == value
    with pytest.raises(ValueError, match="identity or content changed"):
        _read_plan(path, "0" * 64)
    symlink = tmp_path / "plan-link.json"
    symlink.symlink_to(path)
    with pytest.raises(ValueError, match="binding is invalid"):
        _read_plan(symlink, digest)
