"""Bounded, exact-identity desktop management for the authenticated web UI.

Registry layout transactions never restart a terminal. Process operations use
the same backend as the desktop, report individual failures, and protect pins.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid

from .session_store import SessionStore, utc_now
from .tmux_backend import TmuxBackend, TmuxError


class ManagementError(ValueError):
    pass


FIELDS = {
    "list": {"query", "view", "offset", "limit", "window_id", "sort", "descending"},
    "create-window": {"name"}, "rename-window": {"window_id", "name"},
    "reorder": {"window_id", "ids", "expected"},
    "move": {"window_id", "ids", "sources"},
    "bulk": {"action", "ids", "confirmation"},
    "rename": {"ids", "name"}, "capture": {"ids"},
    "appearance": {"mode", "values", "revision", "confirmation"},
}


def text(value, *, maximum=256, empty=False):
    if not isinstance(value, str) or len(value.encode()) > maximum or any(ord(c) < 32 for c in value):
        raise ManagementError("Invalid text")
    if not empty and not value.strip():
        raise ManagementError("A value is required")
    return value.strip()


def identity(value):
    value = text(value)
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", value):
        raise ManagementError("Invalid identity")
    return value


def identities(value, maximum=128):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ManagementError(f"Select between 1 and {maximum} tabs")
    values = [identity(item) for item in value]
    if len(set(values)) != len(values):
        raise ManagementError("Duplicate tab identity")
    return values


def visible(store, window_id):
    return store.list(window_id=window_id, visible_only=True, include_stopped=False)


def write_order(store, records):
    # Pinned tabs remain first, just as in the desktop application.
    ordered = sorted(records, key=lambda row: not row.pinned)
    store.conn.executemany("UPDATE sessions SET tab_index = ? WHERE id = ?", [(i, row.id) for i, row in enumerate(ordered)])


def sampled_tasks(records, backend):
    from .resources import ResourceSampler
    from .settings import AppSettings
    from .task_snapshot import build_task_snapshot
    policy = AppSettings.load().policy_for_record
    sampler = ResourceSampler(snapshot_cache_ttl=0, max_cache_entries=16384)
    first = build_task_snapshot(records, backend=backend, sampler=sampler, policy_resolver=policy)
    if any(item.snapshot.exists for item in first):
        # A fresh CLI process has no previous CPU counter. Two bounded samples
        # avoid a manager that misleadingly displays permanent zero CPU usage.
        time.sleep(0.12)
        return build_task_snapshot(records, backend=backend, sampler=sampler, policy_resolver=policy)
    return first


def handle(store: SessionStore, request: object, *, backend=None):
    if not isinstance(request, dict) or not isinstance(request.get("operation"), str) or request["operation"] not in FIELDS:
        raise ManagementError("Unknown management operation")
    operation = request["operation"]
    if set(request) - FIELDS[operation] - {"operation"}:
        raise ManagementError("Unknown request field")
    if operation == "appearance":
        from .web_appearance import exchange
        return exchange(request)
    if operation == "list":
        return listing(store, request, backend=backend)
    if operation == "create-window":
        name = text(request.get("name"))
        window_id = "win-" + uuid.uuid4().hex
        store.remember_window(window_id, name)
        return {"ok": True, "window_id": window_id}
    if operation in {"rename-window", "reorder", "move"}:
        window_id = identity(request.get("window_id"))
        # Acquire the write lock BEFORE checking identities/order. No partial
        # moves and no stale browser overwriting somebody else's tab order.
        with store.conn:
            store.conn.execute("BEGIN IMMEDIATE")
            windows = dict(store.remembered_windows())
            if window_id not in windows:
                raise ManagementError("Window no longer exists; refresh and retry")
            if operation == "rename-window":
                name = text(request.get("name"))
                store.conn.execute("UPDATE sessions SET window_title = ? WHERE window_id = ?", (name, window_id))
                store.conn.execute("UPDATE remembered_windows SET window_title = ?, last_opened_at = ? WHERE window_id = ?", (name, utc_now(), window_id))
            else:
                ids = identities(request.get("ids"), maximum=4096 if operation == "reorder" else 128)
                rows = [store.get(item) for item in ids]
                if any(row is None for row in rows):
                    raise ManagementError("A selected tab no longer exists")
                if operation == "reorder":
                    current = [row.id for row in visible(store, window_id)]
                    if request.get("expected") != current or set(ids) != set(current):
                        raise ManagementError("Tab order changed; refresh and retry")
                    write_order(store, rows)
                else:
                    sources = request.get("sources")
                    if not isinstance(sources, dict) or sources != {row.id: row.window_id for row in rows}:
                        raise ManagementError("A tab moved since selection; refresh and retry")
                    if any(row.status in {"stopped", "archived"} for row in rows):
                        raise ManagementError("Stopped or archived tabs cannot be moved into a live window")
                    old_windows = {row.window_id for row in rows}
                    destination = [row for row in visible(store, window_id) if row.id not in ids] + rows
                    store.conn.executemany("UPDATE sessions SET window_id = ?, window_title = ?, visible = 1 WHERE id = ?", [(window_id, windows[window_id], item) for item in ids])
                    store.conn.executemany("DELETE FROM window_captains WHERE session_id = ? AND window_id != ?", [(item, window_id) for item in ids])
                    write_order(store, destination)
                    for old in old_windows - {window_id}:
                        write_order(store, visible(store, old))
        return {"ok": True, "window_id": window_id}
    ids = identities(request.get("ids"), maximum=1 if operation in {"rename", "capture"} else 128)
    rows = [store.get(item) for item in ids]
    if any(row is None for row in rows):
        raise ManagementError("A selected tab no longer exists")
    if operation == "rename":
        store.update(ids[0], name=text(request.get("name")))
        return {"ok": True}
    backend = backend or TmuxBackend()
    if operation == "capture":
        from .redaction import redact_text
        return {"ok": True, "capture": redact_text(backend.capture(rows[0], lines=120)[-65536:]).text}
    action = request.get("action")
    if not isinstance(action, str) or action not in {"pause", "resume", "pin", "unpin", "hide", "show", "archive", "kill", "remove", "pause-hot"}:
        raise ManagementError("Unknown bulk action")
    if action in {"pause", "pause-hot", "kill", "remove", "archive"} and request.get("confirmation") != ids:
        raise ManagementError("Confirm the exact selected tabs before continuing")
    hot = set()
    if action == "pause-hot":
        from .health import should_pause_for_attention
        hot = {item.record.id for item in sampled_tasks(rows, backend) if should_pause_for_attention(item.health)}
    results = []
    for selected in rows:
        row = store.get(selected.id)  # Recheck pin/state, never act on a stale list row.
        try:
            if row is None:
                raise ManagementError("Tab no longer exists")
            if row.pinned and action in {"kill", "remove", "hide", "archive", "pause-hot"}:
                raise ManagementError("Pinned tab protected; unpin it first")
            if action == "pause-hot" and row.id not in hot:
                results.append({"id": row.id, "ok": True, "detail": "No hot process; unchanged"})
                continue
            if action in {"pause", "resume", "pause-hot"}:
                backend.pane_pid(row)
                if not backend.available or getattr(backend, "pane_snapshot_stale", False):
                    raise ManagementError("Live process identity is unavailable; no signal sent")
                pid = backend.resume(row) if action == "resume" else backend.pause(row)
                if pid is None:
                    raise ManagementError("No live terminal process")
                store.update(row.id, status="running" if action == "resume" else "paused", last_pid=pid)
            elif action == "kill":
                backend.terminate(row)
                if backend.session_exists(row):
                    raise ManagementError("The terminal is still running; it was not marked stopped")
                store.clear_window_captain(row.window_id, session_id=row.id)
                store.update(row.id, status="stopped", visible=False, last_pid=None, recovery_command=None, recovery_cwd=None, recovery_intent="inactive")
            elif action == "remove":
                # Unknown backend state must not be interpreted as stopped.
                if row.status != "stopped" or not backend.available or backend.pane_pid(row) is not None or getattr(backend, "pane_snapshot_stale", False):
                    raise ManagementError("Only confirmed stopped, unpinned records can be removed")
                store.delete(row.id)
            elif action in {"pin", "unpin"}:
                store.update(row.id, pinned=action == "pin")
            elif action == "show":
                if row.status == "stopped":
                    raise ManagementError("This tab is stopped; it cannot be reopened without restarting")
                if row.status == "archived":
                    from .resources import ResourceSampler
                    pid = backend.pane_pid(row)
                    if not backend.available or pid is None or getattr(backend, "pane_snapshot_stale", False):
                        raise ManagementError("No confirmed live terminal remains for this archived tab")
                    state = ResourceSampler().snapshot(pid)
                    if not state.exists:
                        raise ManagementError("The archived terminal exited before it could be reopened")
                    store.update(row.id, visible=True, status="paused" if state.state in {"T", "t"} else "running", last_pid=pid)
                else:
                    store.update(row.id, visible=True)
            elif action == "archive":
                store.clear_window_captain(row.window_id, session_id=row.id)
                store.update(row.id, status="archived", visible=False)
            elif action == "hide":
                store.clear_window_captain(row.window_id, session_id=row.id)
                store.update(row.id, visible=False, last_opened_at=utc_now())
            with store.conn:
                write_order(store, visible(store, row.window_id))
            results.append({"id": row.id, "ok": True, "detail": action})
        except (TmuxError, ManagementError, OSError, ValueError) as exc:
            results.append({"id": selected.id, "ok": False, "detail": str(exc)[:512]})
    return {"ok": all(row["ok"] for row in results), "results": results}


def listing(store, request, *, backend=None):
    from .task_snapshot import task_snapshot_to_dicts
    query = text(request.get("query", ""), maximum=512, empty=True).casefold()
    view = request.get("view", "all")
    if not isinstance(view, str) or view not in {"all", "visible", "hidden", "history"}:
        raise ManagementError("Invalid manager view")
    offset, limit = request.get("offset", 0), request.get("limit", 100)
    if type(offset) is not int or not 0 <= offset <= 16384 or type(limit) is not int or not 1 <= limit <= 200:
        raise ManagementError("Invalid page")
    window_id = request.get("window_id")
    if window_id is not None:
        window_id = identity(window_id)
    records = store.list(include_archived=True, window_id=window_id)
    if len(records) > 16384:
        raise ManagementError("Registry exceeds the manager safety limit")
    records = [row for row in records if
               (view == "all" or view == "history" and row.status in {"stopped", "archived"}
                or view == "visible" and row.visible and row.status not in {"stopped", "archived"}
                or view == "hidden" and not row.visible and row.status not in {"stopped", "archived"})
               and (not query or query in f"{row.id} {row.name} {row.window_title} {row.tag} {row.command} {row.cwd}".casefold())]
    metadata_keys = {"id": "id", "name": "name", "window": "window_title", "pinned": "pinned", "command": "command", "cwd": "cwd"}
    metric_keys = {"pid", "state", "health", "cpu_percent", "memory_bytes", "process_count", "runtime_seconds", "focus_key", "pause_key"}
    sort = request.get("sort", "")
    if not isinstance(sort, str) or sort not in {"", *metadata_keys, *metric_keys} or type(request.get("descending", False)) is not bool:
        raise ManagementError("Invalid sort")
    if sort in metadata_keys:
        records.sort(key=lambda row: str(getattr(row, metadata_keys[sort])).casefold(), reverse=request.get("descending", False))
    page = records if sort in metric_keys else records[offset:offset + limit]
    items = task_snapshot_to_dicts(sampled_tasks(page, backend))
    for item, row in zip(items, page):
        item.update(window_id=row.window_id, status=row.status)
        # Bound long user commands/paths without dropping rows or identities.
        for key, value in list(item.items()):
            if isinstance(value, str):
                item[key] = value[:2048]
    if sort in metric_keys:
        numeric = sort in {"pid", "cpu_percent", "memory_bytes", "process_count", "runtime_seconds"}
        items.sort(key=lambda item: (item.get(sort) or 0) if numeric else str(item.get(sort) or "").casefold(), reverse=request.get("descending", False))
        items = items[offset:offset + limit]
    return {"ok": True, "rows": items, "total": len(records), "offset": offset,
            "windows": [{"id": wid, "name": name} for wid, name in store.remembered_windows()]}


def command(args):
    try:
        if len(args.request.encode()) > 65536:
            raise ManagementError("Request too large")
        request = json.loads(args.request)
        store = SessionStore(args.db)
        try:
            result = handle(store, request)
        finally:
            store.close()
    except (ValueError, TmuxError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:512]}
    print(json.dumps(result, ensure_ascii=True))
    return 0  # Structured failures, including partial outcomes, reach the UI.
