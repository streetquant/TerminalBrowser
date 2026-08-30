from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    RECOVERY_INTENT_INACTIVE,
    RECOVERY_INTENT_LEGACY,
    SessionRecord,
)
from .session_store import (
    SessionStore,
    normalise_recovery_intent,
    recovery_intent_for_status,
    short_id,
    tmux_safe_name,
)

SNAPSHOT_SCHEMA_VERSION = 2
SUPPORTED_SNAPSHOT_VERSIONS = {1, 2}


class BrowserSnapshotError(ValueError):
    """Raised when a browser-session snapshot cannot be parsed or imported."""


@dataclass(frozen=True)
class SnapshotImportRow:
    original_id: str
    session_id: str
    name: str
    tag: str
    command: str
    cwd: str
    window_id: str
    window_title: str
    tab_index: int
    visible: bool
    pinned: bool
    status: str
    requested_account: str | None
    created: bool
    planned: bool
    recovery_intent: str = RECOVERY_INTENT_LEGACY

    def to_dict(self) -> dict[str, object]:
        return {
            "original_id": self.original_id,
            "session_id": self.session_id,
            "name": self.name,
            "tag": self.tag,
            "command": self.command,
            "cwd": self.cwd,
            "window_id": self.window_id,
            "window_title": self.window_title,
            "tab_index": self.tab_index,
            "visible": self.visible,
            "pinned": self.pinned,
            "status": self.status,
            "requested_account": self.requested_account,
            "created": self.created,
            "planned": self.planned,
            "recovery_intent": self.recovery_intent,
        }


@dataclass(frozen=True)
class SnapshotImportResult:
    name: str
    mode: str
    dry_run: bool
    count: int
    rows: tuple[SnapshotImportRow, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "count": self.count,
            "sessions": [row.to_dict() for row in self.rows],
        }


_SAFE_STATUS = {"new", "running", "paused", "stopped", "archived", "recovery-pending", "start-error"}
_CWD_TOKEN = re.compile(r"^~|\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_status(status: Any, *, restart_stopped: bool = False) -> str:
    value = str(status or "new").strip().lower()
    if value not in _SAFE_STATUS:
        value = "new"
    if value == "archived":
        value = "stopped"
    if value == "stopped" and restart_stopped:
        value = "new"
    return value


def _normalise_recovery_intent(value: Any, *, status: str) -> str:
    if status in {"new", "remembered", "archived"}:
        return RECOVERY_INTENT_INACTIVE
    if value is None:
        return recovery_intent_for_status(status)
    return normalise_recovery_intent(value)


def _record_to_tab(record: SessionRecord) -> dict[str, object]:
    return {
        "session_id": record.id,
        "name": record.name,
        "tag": record.tag,
        "command": record.command,
        "cwd": record.cwd,
        "status": record.status,
        "visible": record.visible,
        "pinned": record.pinned,
        "tab_index": record.tab_index,
        "last_pid": record.last_pid,
        "created_at": record.created_at,
        "last_opened_at": record.last_opened_at,
        "selected_account": record.selected_account,
        "recovery_intent": record.recovery_intent,
    }


def build_browser_snapshot(
    records: list[SessionRecord] | tuple[SessionRecord, ...],
    *,
    name: str = "termdeck-browser-snapshot",
    include_hidden: bool = True,
    include_stopped: bool = True,
) -> dict[str, object]:
    """Return a local backup of remembered browser windows and terminal tabs.

    Workspace recipes are intentionally shareable and omit runtime state. Browser
    snapshots are local backups: they preserve window IDs, visible/hidden state,
    stopped/running/paused status, session IDs, and tab order.  The tmux session
    name is deliberately omitted so importing the snapshot cannot collide with
    live tmux sessions on another machine.
    """

    filtered = [
        record
        for record in records
        if (include_hidden or record.visible) and (include_stopped or record.status != "stopped") and record.status != "archived"
    ]
    grouped: dict[str, list[SessionRecord]] = {}
    titles: dict[str, str] = {}
    for record in filtered:
        grouped.setdefault(record.window_id, []).append(record)
        titles.setdefault(record.window_id, record.window_title or "Window")

    windows: list[dict[str, object]] = []
    for window_id in sorted(grouped, key=lambda wid: (titles.get(wid, ""), wid)):
        rows = sorted(grouped[window_id], key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at, row.id))
        windows.append(
            {
                "window_id": window_id,
                "title": titles.get(window_id, "Window"),
                "tabs": [_record_to_tab(row) for row in rows],
            }
        )

    return {
        "schema": "termdeck.browser-snapshot",
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "name": name or "termdeck-browser-snapshot",
        "generated_at": _utc_now(),
        "counts": {
            "windows": len(windows),
            "tabs": sum(len(window["tabs"]) for window in windows),
            "hidden_tabs": sum(1 for record in filtered if not record.visible),
            "pinned_tabs": sum(1 for record in filtered if record.pinned),
            "stopped_tabs": sum(1 for record in filtered if record.status == "stopped"),
        },
        "windows": windows,
    }


def browser_snapshot_json(
    records: list[SessionRecord] | tuple[SessionRecord, ...],
    *,
    name: str = "termdeck-browser-snapshot",
    include_hidden: bool = True,
    include_stopped: bool = True,
) -> str:
    return json.dumps(
        build_browser_snapshot(records, name=name, include_hidden=include_hidden, include_stopped=include_stopped),
        indent=2,
        sort_keys=True,
    )


def _expect_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BrowserSnapshotError(f"{field} must be an object")
    return value


def _expect_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise BrowserSnapshotError(f"{field} must be an array")
    return value


def _string(value: Any, field: str, *, default: str | None = None) -> str:
    if value is None:
        if default is None:
            raise BrowserSnapshotError(f"{field} is required")
        return default
    if not isinstance(value, str):
        raise BrowserSnapshotError(f"{field} must be a string")
    value = value.strip()
    if not value and default is None:
        raise BrowserSnapshotError(f"{field} cannot be empty")
    return value or (default or "")


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BrowserSnapshotError(f"{field} must be a string or null")
    value = value.strip()
    return value or None


def _bool(value: Any, field: str, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise BrowserSnapshotError(f"{field} must be true or false")


def _int(value: Any, field: str, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrowserSnapshotError(f"{field} must be an integer")
    return max(0, value)


def _normalise_cwd(value: Any, field: str) -> str:
    cwd = _string(value, field, default=str(Path.home()))
    return str(Path(cwd).expanduser().resolve(strict=False))


def load_snapshot(path: str | Path) -> dict[str, Any]:
    snapshot_path = Path(path).expanduser().resolve(strict=False)
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrowserSnapshotError(f"browser snapshot not found: {snapshot_path}") from exc
    except json.JSONDecodeError as exc:
        raise BrowserSnapshotError(f"invalid JSON in browser snapshot: {exc}") from exc
    return parse_snapshot(data)


def parse_snapshot(data: Any) -> dict[str, Any]:
    root = _expect_dict(data, "snapshot")
    if root.get("schema") != "termdeck.browser-snapshot":
        raise BrowserSnapshotError("snapshot.schema must be 'termdeck.browser-snapshot'")
    source_version = int(root.get("schema_version", 0))
    if source_version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise BrowserSnapshotError(f"unsupported browser snapshot schema_version: {root.get('schema_version')!r}")
    windows = _expect_list(root.get("windows"), "windows")
    parsed_windows: list[dict[str, object]] = []
    for window_index, raw_window in enumerate(windows):
        window = _expect_dict(raw_window, f"windows[{window_index}]")
        title = _string(window.get("title"), f"windows[{window_index}].title", default=f"Window {window_index + 1}")
        window_id = _string(window.get("window_id"), f"windows[{window_index}].window_id", default=tmux_safe_name(title) or f"window-{window_index + 1}")
        tabs = _expect_list(window.get("tabs"), f"windows[{window_index}].tabs")
        parsed_tabs: list[dict[str, object]] = []
        for tab_index, raw_tab in enumerate(tabs):
            tab = _expect_dict(raw_tab, f"windows[{window_index}].tabs[{tab_index}]")
            command = _string(tab.get("command"), f"windows[{window_index}].tabs[{tab_index}].command")
            raw_status = str(tab.get("status") or "new").strip().lower()
            status = _normalise_status(tab.get("status"))
            parsed_tabs.append(
                {
                    "original_id": _string(tab.get("session_id"), f"windows[{window_index}].tabs[{tab_index}].session_id", default=f"snapshot-{window_index + 1}-{tab_index + 1}"),
                    "name": _string(tab.get("name"), f"windows[{window_index}].tabs[{tab_index}].name", default=command.split()[0] if command.split() else "task"),
                    "tag": _string(tab.get("tag"), f"windows[{window_index}].tabs[{tab_index}].tag", default="default"),
                    "command": command,
                    "cwd": _normalise_cwd(tab.get("cwd"), f"windows[{window_index}].tabs[{tab_index}].cwd"),
                    "status": status,
                    "visible": _bool(tab.get("visible"), f"windows[{window_index}].tabs[{tab_index}].visible", default=True),
                    "pinned": _bool(tab.get("pinned"), f"windows[{window_index}].tabs[{tab_index}].pinned", default=False),
                    "tab_index": _int(tab.get("tab_index"), f"windows[{window_index}].tabs[{tab_index}].tab_index", default=tab_index),
                    "selected_account": _optional_string(tab.get("selected_account"), f"windows[{window_index}].tabs[{tab_index}].selected_account"),
                    "recovery_intent": _normalise_recovery_intent(
                        tab.get("recovery_intent"),
                        # Archived is represented as stopped in the import
                        # layout for compatibility, but its explicit intent
                        # must remain inactive rather than inheriting a
                        # caller-supplied active value.
                        status=("archived" if raw_status == "archived" else status),
                    ),
                }
            )
        parsed_windows.append({"window_id": window_id, "title": title, "tabs": parsed_tabs})
    return {
        "schema": "termdeck.browser-snapshot",
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "name": _string(root.get("name"), "name", default="termdeck-browser-snapshot"),
        "generated_at": _string(root.get("generated_at"), "generated_at", default=""),
        "windows": parsed_windows,
    }


def _import_window_id(snapshot_name: str, source_window_id: str, window_index: int, *, preserve_window_ids: bool) -> str:
    if preserve_window_ids:
        return source_window_id
    base = tmux_safe_name(f"{snapshot_name}_{window_index + 1:02d}").lower().strip("_") or "snapshot"
    return f"{base}_{short_id('win').replace('-', '_')}"


def import_browser_snapshot(
    store: SessionStore,
    snapshot: dict[str, Any],
    *,
    mode: str = "append",
    dry_run: bool = False,
    visible_only: bool = False,
    preserve_window_ids: bool = False,
    restart_stopped: bool = False,
) -> SnapshotImportResult:
    """Import a browser snapshot into the remembered session DB without tmux.

    Importing does not start processes. It only rebuilds the browser restore set.
    This keeps snapshot restore safe in CI and on machines where tmux is missing.
    """

    parsed = parse_snapshot(snapshot)
    clean_mode = (mode or "append").lower()
    if clean_mode not in {"append", "replace"}:
        raise BrowserSnapshotError("mode must be 'append' or 'replace'")

    rows: list[SnapshotImportRow] = []
    if clean_mode == "replace" and not dry_run:
        for record in store.list(include_archived=True):
            store.update(record.id, status="archived", visible=False)

    for window_index, window in enumerate(parsed["windows"]):
        window_id = _import_window_id(str(parsed["name"]), str(window["window_id"]), window_index, preserve_window_ids=preserve_window_ids)
        title = str(window["title"])
        for fallback_tab_index, tab in enumerate(window["tabs"]):
            visible = bool(tab["visible"])
            if visible_only and not visible:
                continue
            status = _normalise_status(tab["status"], restart_stopped=restart_stopped)
            recovery_intent = _normalise_recovery_intent(
                tab.get("recovery_intent"),
                status=status,
            )
            if status == "new":
                recovery_intent = RECOVERY_INTENT_INACTIVE
            tab_index = int(tab.get("tab_index", fallback_tab_index))
            if dry_run:
                session_id = f"planned-{window_index + 1:02d}-{fallback_tab_index + 1:02d}"
                created = False
            else:
                record = store.create(
                    name=str(tab["name"]),
                    tag=str(tab["tag"]),
                    command=str(tab["command"]),
                    cwd=str(tab["cwd"]),
                    window_id=window_id,
                    window_title=title,
                    tab_index=tab_index,
                    visible=visible,
                    pinned=bool(tab.get("pinned", False)),
                )
                requested_account = tab.get("selected_account")
                record = store.update(
                    record.id,
                    status=status,
                    last_pid=None,
                    selected_account=None,
                    pending_account=str(requested_account) if requested_account else None,
                    auth_restart_required=False,
                    recovery_intent=recovery_intent,
                )
                session_id = record.id
                created = True
            rows.append(
                SnapshotImportRow(
                    original_id=str(tab["original_id"]),
                    session_id=session_id,
                    name=str(tab["name"]),
                    tag=str(tab["tag"]),
                    command=str(tab["command"]),
                    cwd=str(tab["cwd"]),
                    window_id=window_id,
                    window_title=title,
                    tab_index=tab_index,
                    visible=visible,
                    pinned=bool(tab.get("pinned", False)),
                    status=status,
                    requested_account=str(tab["selected_account"]) if tab.get("selected_account") else None,
                    created=created,
                    planned=dry_run,
                    recovery_intent=recovery_intent,
                )
            )
    return SnapshotImportResult(name=str(parsed["name"]), mode=clean_mode, dry_run=dry_run, count=len(rows), rows=tuple(rows))


def restore_plan(records: list[SessionRecord] | tuple[SessionRecord, ...], *, limit: int = 50) -> dict[str, object]:
    """Describe the browser tabs that should reopen without starting anything."""

    visible = [record for record in records if record.visible and record.status not in {"stopped", "archived"}]
    rows = visible[: max(1, int(limit))]
    grouped: dict[str, list[SessionRecord]] = {}
    titles: dict[str, str] = {}
    for record in rows:
        grouped.setdefault(record.window_id, []).append(record)
        titles.setdefault(record.window_id, record.window_title or "Window")
    windows: list[dict[str, object]] = []
    for window_id in sorted(grouped, key=lambda wid: (titles.get(wid, ""), wid)):
        ordered = sorted(grouped[window_id], key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at, row.id))
        windows.append(
            {
                "window_id": window_id,
                "title": titles.get(window_id, "Window"),
                "tabs": [
                    {
                        "session_id": record.id,
                        "name": record.name,
                        "tag": record.tag,
                        "command": record.command,
                        "cwd": record.cwd,
                        "status": record.status,
                        "tab_index": record.tab_index,
                        "visible": record.visible,
                        "pinned": record.pinned,
                        "reason": "visible and not stopped",
                    }
                    for record in ordered
                ],
            }
        )
    skipped = [
        {"session_id": record.id, "name": record.name, "tag": record.tag, "status": record.status, "visible": record.visible, "pinned": record.pinned, "reason": "hidden" if not record.visible else "stopped or archived"}
        for record in records
        if record not in rows
    ]
    return {"count": len(rows), "window_count": len(windows), "windows": windows, "skipped": skipped}
