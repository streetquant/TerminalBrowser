"""Automatic tab names return on an empty rename; explicit names stay put."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sqlite3

import pytest

from termdeck.command_render import directory_tab_name
from termdeck.session_launcher import create_new_session, resolve_launch_fields
from termdeck.session_lifecycle import move_records, rename_record
from termdeck.session_store import SessionStore
from termdeck.web_management import ManagementError, handle


@pytest.fixture
def automatic_tab(tmp_path: Path):
    launch = tmp_path / "arch"
    launch.mkdir()
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    # `t1000 new` without --name stores the launch directory's automatic name.
    record = store.create(directory_tab_name(str(launch)), "default", "bash", str(launch), automatic_name=True, window_id="one", window_title="One", tab_index=0)
    store.update(record.id, status="running", last_pid=77)
    yield store, store.get(record.id)
    store.close()


def test_explicit_rename_is_kept_and_empty_rename_restores_automatic_name(automatic_tab):
    store, record = automatic_tab
    assert record.name == "arch"

    renamed = rename_record(store, record, "  outreach  ")
    assert renamed.ok
    assert store.get(record.id).name == "outreach"
    assert store.get(record.id).automatic_name is False

    restored = rename_record(store, store.get(record.id), "   ")
    assert restored.ok
    assert "automatic" in restored.detail
    after = store.get(record.id)
    assert after.name == directory_tab_name(after.cwd) == "arch"
    assert after.automatic_name is True
    assert after.cwd == record.cwd
    assert after.last_pid == 77
    assert after.status == "running"


def test_management_rename_accepts_empty_name_as_automatic(automatic_tab):
    store, record = automatic_tab
    handle(store, {"operation": "rename", "ids": [record.id], "name": "Manual"})
    assert store.get(record.id).name == "Manual"
    assert store.get(record.id).automatic_name is False
    handle(store, {"operation": "rename", "ids": [record.id], "name": ""})
    assert store.get(record.id).name == "arch"
    assert store.get(record.id).automatic_name is True
    with pytest.raises(ManagementError, match="Invalid text"):
        handle(store, {"operation": "rename", "ids": [record.id], "name": "bad\x07"})
    assert store.get(record.id).name == "arch"


@pytest.mark.parametrize("surface", ["lifecycle", "management"])
def test_explicit_same_basename_is_manual_and_stays_manual(automatic_tab, surface):
    store, record = automatic_tab
    if surface == "lifecycle":
        assert rename_record(store, record, "arch").ok
    else:
        with pytest.raises(ManagementError, match="Unknown request field"):
            handle(store, {"operation": "rename", "ids": [record.id], "name": "arch",
                           "automatic_name": True, "cwd": "/client/untrusted"})
        assert store.get(record.id).automatic_name is True
        handle(store, {"operation": "rename", "ids": [record.id], "name": "arch"})
    manual = store.get(record.id)
    assert manual.automatic_name is False
    assert manual.name_is_automatic is False
    assert manual.cwd == record.cwd
    updated = store.update(record.id, cwd="/server/another")
    assert updated.name == "arch"
    assert updated.automatic_name is False
    assert updated.tmux_name == record.tmux_name


@pytest.mark.parametrize("explicit_name", [None, "arch"])
def test_launch_records_user_intent_even_for_matching_basename(automatic_tab, explicit_name):
    store, record = automatic_tab
    fields = resolve_launch_fields(cwd=record.cwd, name=explicit_name)
    result = create_new_session(store, object(), fields, start=False)
    created = store.get(result.id)
    assert created.name == "arch"
    assert created.automatic_name is (explicit_name is None)
    # Callers supplying a name directly to the store create manual names.
    direct = store.create("arch", "terminal", "bash", record.cwd)
    assert direct.automatic_name is False


def test_tab_moves_and_reopen_preserve_naming_intent_and_process(automatic_tab):
    store, automatic = automatic_tab
    manual = store.create("arch", "terminal", "bash", automatic.cwd, window_id="one")
    store.update(manual.id, status="running", last_pid=88)
    assert all(result.ok for result in move_records(
        store, [automatic, manual], window_id="two", window_title="Two"
    ))
    reopened = SessionStore(store.path, provision_auth_scopes=False)
    try:
        for original, expected, pid in [(automatic, True, 77), (manual, False, 88)]:
            moved = reopened.get(original.id)
            assert moved.window_id == "two"
            assert moved.automatic_name is expected
            assert moved.tmux_name == original.tmux_name
            assert moved.cwd == original.cwd
            assert moved.last_pid == pid
            assert moved.status == "running"
    finally:
        reopened.close()


def test_nullable_migration_preserves_legacy_rows_and_explicit_intent(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    launch = tmp_path / "arch"
    launch.mkdir()
    store = SessionStore(path, provision_auth_scopes=False)
    auto = store.create("arch", "terminal", "bash", str(launch), automatic_name=True)
    manual = store.create("outreach", "terminal", "bash", str(launch))
    before = {row["id"]: dict(row) for row in store.conn.execute("SELECT * FROM sessions")}
    store.conn.execute("ALTER TABLE sessions DROP COLUMN automatic_name")
    store.conn.commit()
    store.close()

    for pass_number in range(2):
        store = SessionStore(path, provision_auth_scopes=False)
        try:
            assert len(store.list()) == 2
            for original in [auto, manual]:
                after = dict(store.conn.execute("SELECT * FROM sessions WHERE id = ?", (original.id,)).fetchone())
                assert after.pop("automatic_name") is None
                expected = dict(before[original.id])
                expected.pop("automatic_name")
                assert after == expected, f"migration changed existing fields on pass {pass_number}"
            assert store.get(auto.id).name_is_automatic is True
            assert store.get(manual.id).name_is_automatic is False
        finally:
            store.close()

    store = SessionStore(path, provision_auth_scopes=False)
    try:
        # Adoption/recovery may upsert an old row with a newly observed cwd.
        # Freeze its prior inference before that cwd changes underneath it.
        legacy_auto = replace(store.get(auto.id), cwd=str(tmp_path / "another"))
        store.upsert(legacy_auto)
        assert store.get(auto.id).name_is_automatic is True
        assert store.get(auto.id).automatic_name is True
        assert rename_record(store, store.get(auto.id), "arch").ok
        # An old snapshot without the field cannot erase the explicit choice.
        legacy = replace(store.get(auto.id), automatic_name=None)
        store.upsert(legacy)
        assert store.get(auto.id).automatic_name is False
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE sessions SET automatic_name = 7 WHERE id = ?", (auto.id,))
    finally:
        store.close()
