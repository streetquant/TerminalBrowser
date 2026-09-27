from unittest.mock import Mock

import pytest

from termdeck.session_store import SessionStore
from termdeck.web_management import ManagementError, handle


@pytest.fixture
def registry(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.remember_window("one", "One")
    store.remember_window("two", "Two")
    rows = [store.create(f"tab-{i}", "test", "bash", str(tmp_path), window_id="one", window_title="One", tab_index=i) for i in range(3)]
    yield store, rows
    store.close()


def test_empty_window_creation_and_rename_preserve_process_fields(registry):
    store, rows = registry
    created = handle(store, {"operation": "create-window", "name": "Empty window"})
    assert (created["window_id"], "Empty window") in store.remembered_windows()
    before = [store.get(row.id).__dict__ for row in rows]
    handle(store, {"operation": "rename-window", "window_id": "one", "name": "Renamed λ"})
    for old, row in zip(before, rows):
        new = store.get(row.id).__dict__
        assert new == {**old, "window_title": "Renamed λ"}


def test_window_creation_receipt_survives_reopen_and_name_change(registry):
    store, rows = registry
    request = {"operation": "create-window", "name": "New", "request_id": "a" * 32, "_actor_session": "session-one"}
    before = [store.get(row.id).__dict__ for row in rows]
    created = handle(store, request)
    handle(store, {"operation": "rename-window", "window_id": created["window_id"], "name": "Renamed"})
    replay = handle(store, request)
    assert replay == {**created, "window_name": "Renamed", "replayed": True}
    with pytest.raises(ManagementError, match="different name"):
        handle(store, {**request, "name": "Another"})
    separate = handle(store, {**request, "_actor_session": "session-two"})
    assert separate["window_id"] != created["window_id"]
    assert [store.get(row.id).__dict__ for row in rows] == before
    store.conn.execute("DELETE FROM remembered_windows WHERE window_id=?", (created["window_id"],))
    store.conn.commit()
    with pytest.raises(ManagementError, match="no longer available"):
        handle(store, request)


def test_concurrent_empty_window_creation_is_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "sessions.sqlite3"
    SessionStore(path).close()
    request = {"operation": "create-window", "name": "Concurrent", "request_id": "b" * 32, "_actor_session": "session-one"}
    def create(_):
        store = SessionStore(path)
        try:
            return handle(store, request)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(8)))
    assert len({row["window_id"] for row in results}) == 1
    assert sum(not row.get("replayed", False) for row in results) == 1
    store = SessionStore(path)
    try:
        assert len(store.remembered_windows()) == 1
        assert handle(store, request)["replayed"]
    finally:
        store.close()


@pytest.mark.parametrize("request_id", [None, 123, "a" * 31, "A" * 32, "../oops", "a" * 33])
def test_creation_rejects_invalid_receipt_without_mutation(registry, request_id):
    store, _ = registry
    before = store.remembered_windows()
    with pytest.raises(ManagementError, match="request identity"):
        handle(store, {"operation": "create-window", "name": "New", "request_id": request_id})
    assert store.remembered_windows() == before


def test_reorder_is_exact_and_stale_orders_roll_back(registry):
    store, rows = registry
    ids = [row.id for row in rows]
    handle(store, {"operation": "reorder", "window_id": "one", "expected": ids, "ids": ids[::-1]})
    assert [row.id for row in store.visible_records()] == ids[::-1]
    with pytest.raises(ManagementError, match="order changed"):
        handle(store, {"operation": "reorder", "window_id": "one", "expected": ids, "ids": ids})
    assert [row.id for row in store.visible_records()] == ids[::-1]


def test_move_is_atomic_and_preserves_session_identity(registry):
    store, rows = registry
    first = store.update(rows[0].id, status="running", last_pid=12345, pinned=True)
    store.set_window_captain("one", first.id)
    before = first.__dict__
    handle(store, {"operation": "move", "window_id": "two", "ids": [first.id], "sources": {first.id: "one"}})
    after = store.get(first.id).__dict__
    assert after == {**before, "window_id": "two", "window_title": "Two", "visible": True, "tab_index": 0}
    assert store.captain_for_window("one") is None
    assert [row.tab_index for row in store.visible_records() if row.window_id == "one"] == [0, 1]


def test_move_stale_source_or_missing_target_does_not_mutate(registry):
    store, rows = registry
    before = [store.get(row.id).__dict__ for row in rows]
    for destination, sources in [("missing", {rows[0].id: "one"}), ("two", {rows[0].id: "wrong"})]:
        with pytest.raises(ManagementError):
            handle(store, {"operation": "move", "window_id": destination, "ids": [rows[0].id], "sources": sources})
        assert [store.get(row.id).__dict__ for row in rows] == before


def test_partial_bulk_outcomes_pin_protection_and_confirmation(registry):
    store, rows = registry
    store.update(rows[0].id, pinned=True)
    ids = [row.id for row in rows]
    result = handle(store, {"operation": "bulk", "action": "hide", "ids": ids})
    assert not result["ok"]
    assert [row["ok"] for row in result["results"]] == [False, True, True]
    assert store.get(ids[0]).visible
    backend = Mock()
    with pytest.raises(ManagementError, match="Confirm"):
        handle(store, {"operation": "bulk", "action": "kill", "ids": ids, "confirmation": ids[1:]}, backend=backend)
    backend.terminate.assert_not_called()


def test_bulk_pause_reports_no_live_process_without_changing_state(registry):
    store, rows = registry
    backend = Mock(); backend.pause.return_value = None
    request = {"operation": "bulk", "action": "pause", "ids": [rows[0].id], "confirmation": [rows[0].id]}
    result = handle(store, request, backend=backend)
    assert not result["ok"]
    assert store.get(rows[0].id).status == rows[0].status


def test_remove_requires_confirmed_stopped_unpinned_record(registry):
    store, rows = registry
    backend = Mock(available=True, pane_snapshot_stale=False); backend.pane_pid.return_value = None
    ids = [rows[0].id]
    request = {"operation": "bulk", "action": "remove", "ids": ids, "confirmation": ids}
    assert not handle(store, request, backend=backend)["ok"]
    store.update(ids[0], status="stopped", visible=False)
    backend.pane_snapshot_stale = True
    assert not handle(store, request, backend=backend)["ok"]
    backend.pane_snapshot_stale = False
    assert handle(store, request, backend=backend)["ok"]
    assert store.get(ids[0]) is None


def test_list_search_history_and_pagination(registry):
    store, rows = registry
    backend = Mock(available=False)
    store.update(rows[2].id, status="stopped", visible=False)
    result = handle(store, {"operation": "list", "limit": 1, "offset": 1}, backend=backend)
    assert result["total"] == 3 and len(result["rows"]) == 1
    assert result["rows"][0]["window_id"] == "one"
    result = handle(store, {"operation": "list", "view": "history"}, backend=backend)
    assert [row["id"] for row in result["rows"]] == [rows[2].id]
    result = handle(store, {"operation": "list", "query": "TAB-1"}, backend=backend)
    assert [row["id"] for row in result["rows"]] == [rows[1].id]


@pytest.mark.parametrize("payload", [None, [], {}, {"operation": "launch"}, {"operation": "create-window", "name": "x", "command": "bash"}, {"operation": "create-window", "name": "\x00"}, {"operation": "list", "offset": True}, {"operation": "list", "limit": 100000}, {"operation": "list", "window_id": "../escape"}, {"operation": "bulk", "action": "send", "ids": ["a"]}, {"operation": "bulk", "action": "pin", "ids": []}])
def test_invalid_requests_are_closed(registry, payload):
    with pytest.raises(ManagementError):
        handle(registry[0], payload)


def test_unknown_ids_are_not_prefix_or_name_selectors(registry):
    store, rows = registry
    with pytest.raises(ManagementError, match="no longer exists"):
        handle(store, {"operation": "bulk", "action": "pin", "ids": [rows[0].name]})
    assert not store.get(rows[0].id).pinned


def test_sorting_applies_before_pagination(registry):
    store, rows = registry
    result = handle(store, {"operation": "list", "sort": "name", "descending": True, "limit": 1}, backend=Mock(available=False))
    assert result["rows"][0]["id"] == rows[2].id
    assert result["total"] == 3


def test_cpu_uses_two_samples_with_one_sampler(registry, monkeypatch):
    from termdeck.web_management import sampled_tasks
    first = Mock(); first.snapshot.exists = True
    second = Mock(); second.snapshot.cpu_percent = 42
    sampler = Mock(side_effect=[[first], [second]])
    delay = Mock()
    monkeypatch.setattr("termdeck.task_snapshot.build_task_snapshot", sampler)
    monkeypatch.setattr("termdeck.web_management.time.sleep", delay)
    assert sampled_tasks(registry[1], Mock()) == [second]
    assert sampler.call_count == 2
    assert sampler.call_args_list[0].kwargs["sampler"] is sampler.call_args_list[1].kwargs["sampler"]
    delay.assert_called_once_with(.12)


def test_failed_kill_does_not_mark_live_tab_stopped(registry):
    store, rows = registry
    backend = Mock(); backend.session_exists.return_value = True
    ids = [rows[0].id]
    result = handle(store, {"operation": "bulk", "action": "kill", "ids": ids, "confirmation": ids}, backend=backend)
    assert not result["ok"]
    assert store.get(ids[0]).status == rows[0].status


def test_window_kill_cascades_all_saved_tabs_and_preserves_external_tmux(registry):
    store, rows = registry
    store.update(rows[0].id, status="running", last_pid=1234)
    store.update(rows[1].id, status="archived", visible=False)
    store.update(rows[2].id, external=True, visible=False)
    other = store.create("other", "test", "bash", "/tmp", window_id="two", window_title="Two")
    backend = Mock(available=True)
    backend.session_exists.return_value = False
    preview = handle(store, {"operation": "preview-kill-window", "window_id": "one"})
    assert {tab["id"] for tab in preview["tabs"]} == {row.id for row in rows}
    assert len(preview["expected"]) == 64
    assert len([tab for tab in preview["tabs"] if tab["external"]]) == 1
    request = {"operation": "kill-window", "window_id": "one", "window_name": "One",
               "expected": preview["expected"], "request_id": "c" * 32, "_actor_session": "session-one"}
    result = handle(store, request, backend=backend)
    assert result["ok"] and result["forgotten"] and not result["remaining"]
    assert all(store.get(row.id) is None for row in rows)
    assert store.get(other.id) is not None and store.remembered_windows() == [("two", "Two")]
    assert {call.args[0].id for call in backend.terminate.call_args_list} == {rows[0].id, rows[1].id}
    assert handle(store, request, backend=backend) == {**result, "replayed": True}
    assert backend.terminate.call_count == 2


def test_window_kill_requires_exact_current_tabs_and_name(registry):
    store, rows = registry
    backend = Mock(available=True)
    preview = handle(store, {"operation": "preview-kill-window", "window_id": "one"})
    base = {"operation": "kill-window", "window_id": "one", "window_name": "One",
            "expected": preview["expected"], "request_id": "d" * 32}
    for changes in ({"window_name": "Wrong"}, {"expected": preview["expected"][:-1]},
                    {"expected": "0" * 64}, {"expected": []},
                    {"request_id": "invalid"}, {"window_id": "../one"}):
        with pytest.raises(ManagementError):
            handle(store, {**base, **changes}, backend=backend)
    backend.terminate.assert_not_called()
    assert len(store.list(window_id="one")) == len(rows)
    store.create("new", "test", "bash", "/tmp", window_id="one", window_title="One")
    with pytest.raises(ManagementError, match="changed"):
        handle(store, base, backend=backend)
    backend.terminate.assert_not_called()
    with pytest.raises(ManagementError, match="no longer exists"):
        handle(store, {"operation": "preview-kill-window", "window_id": "missing"})


def test_window_kill_failure_keeps_window_and_failed_tab_for_retry(registry):
    store, rows = registry
    for row in rows:
        store.update(row.id, status="running")
    backend = Mock(available=True)
    backend.session_exists.return_value = False
    preview = handle(store, {"operation": "preview-kill-window", "window_id": "one"})
    request = {"operation": "kill-window", "window_id": "one", "window_name": "One",
               "expected": preview["expected"], "request_id": "e" * 32}
    # Real backend failures are TmuxError; use that exact error type in the fixture.
    from termdeck.tmux_backend import TmuxError
    backend.terminate.side_effect = [None, TmuxError("fixture backend failure"), None]
    result = handle(store, request, backend=backend)
    assert not result["ok"] and not result["forgotten"] and len(result["remaining"]) == 1
    assert store.remembered_windows()[0] == ("one", "One")
    assert store.get(result["remaining"][0]).status == "running"
    assert handle(store, request, backend=backend)["replayed"]
    assert backend.terminate.call_count == 3
    retry_preview = handle(store, {"operation": "preview-kill-window", "window_id": "one"})
    retry = handle(store, {**request, "expected": retry_preview["expected"], "request_id": "f" * 32}, backend=Mock(available=True, session_exists=Mock(return_value=False)))
    assert retry["ok"] and store.remembered_windows() == [("two", "Two")]


def test_window_kill_missing_tmux_keeps_recoverable_tabs(registry):
    store, rows = registry
    store.update(rows[0].id, status="running", recovery_command="bash", recovery_cwd="/tmp")
    preview = handle(store, {"operation": "preview-kill-window", "window_id": "one"})
    backend = Mock(available=False)
    result = handle(store, {"operation": "kill-window", "window_id": "one", "window_name": "One",
                            "expected": preview["expected"], "request_id": "0" * 32}, backend=backend)
    assert not result["ok"] and rows[0].id in result["remaining"]
    assert store.get(rows[0].id).recovery_command == "bash"
    backend.terminate.assert_not_called()


def test_move_sql_failure_rolls_back_every_tab(registry):
    store, rows = registry
    store.conn.execute(f"CREATE TRIGGER fail_second_move BEFORE UPDATE OF window_id ON sessions WHEN OLD.id = '{rows[1].id}' BEGIN SELECT RAISE(ABORT, 'fixture move failure'); END")
    store.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="fixture move failure"):
        handle(store, {"operation": "move", "window_id": "two", "ids": [rows[0].id, rows[1].id], "sources": {rows[0].id: "one", rows[1].id: "one"}})
    assert all(store.get(row.id).window_id == "one" for row in rows)
