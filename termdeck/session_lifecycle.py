from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import SessionRecord
from .session_store import SessionStore, utc_now


@dataclass(frozen=True)
class LifecycleResult:
    """Result for a browser-tab lifecycle action.

    These actions change TermDeck's browser/session registry. They deliberately
    do not expose tmux wording to callers and, except for explicit process
    controls implemented elsewhere, do not signal or kill terminal tasks.
    """

    id: str
    name: str
    tag: str
    action: str
    ok: bool
    detail: str
    status: str
    visible: bool
    window_id: str
    window_title: str
    tab_index: int
    pinned: bool

    @classmethod
    def from_record(cls, record: SessionRecord, action: str, ok: bool = True, detail: str = "") -> "LifecycleResult":
        return cls(
            id=record.id,
            name=record.name,
            tag=record.tag,
            action=action,
            ok=ok,
            detail=detail,
            status=record.status,
            visible=record.visible,
            window_id=record.window_id,
            window_title=record.window_title,
            tab_index=record.tab_index,
            pinned=record.pinned,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "action": self.action,
            "ok": self.ok,
            "detail": self.detail,
            "status": self.status,
            "visible": self.visible,
            "window_id": self.window_id,
            "window_title": self.window_title,
            "tab_index": self.tab_index,
            "pinned": self.pinned,
        }


def lifecycle_payload(action: str, results: Iterable[LifecycleResult]) -> dict[str, object]:
    rows = [row.as_dict() for row in results]
    return {
        "action": action,
        "count": len(rows),
        "ok": bool(rows) and all(bool(row["ok"]) for row in rows),
        "sessions": rows,
    }


def next_tab_index(store: SessionStore, window_id: str) -> int:
    records = store.list(include_archived=True, visible_only=True, window_id=window_id)
    if not records:
        return 0
    return max(record.tab_index for record in records) + 1


def compact_window(store: SessionStore, window_id: str) -> list[SessionRecord]:
    """Renumber visible tabs in one window without changing process state.

    Pinned tabs are kept at the front of the browser-like tab strip. This mirrors
    the familiar browser model and makes pinned task capsules harder to lose in
    large workspaces.
    """

    records = sorted(
        store.list(include_archived=True, visible_only=True, window_id=window_id),
        key=lambda record: (not record.pinned, record.tab_index, record.last_opened_at, record.name.casefold()),
    )
    updated: list[SessionRecord] = []
    for index, record in enumerate(records):
        if record.tab_index != index:
            record = store.update(record.id, tab_index=index)
        updated.append(record)
    return updated


def _protected_result(record: SessionRecord, action: str) -> LifecycleResult:
    return LifecycleResult.from_record(
        record,
        action,
        ok=False,
        detail="pinned tab protected from this bulk lifecycle action; pass --include-pinned to override",
    )


def hide_records(store: SessionStore, records: Iterable[SessionRecord], *, include_pinned: bool = False) -> list[LifecycleResult]:
    results: list[LifecycleResult] = []
    touched_windows: set[str] = set()
    for record in records:
        if record.pinned and not include_pinned:
            results.append(_protected_result(record, "hide"))
            continue
        updated = store.update(record.id, visible=False, last_opened_at=utc_now())
        touched_windows.add(record.window_id)
        results.append(LifecycleResult.from_record(updated, "hide", detail="hidden from browser restore; process state unchanged"))
    for window_id in touched_windows:
        compact_window(store, window_id)
    return results


def show_records(
    store: SessionStore,
    records: Iterable[SessionRecord],
    *,
    window_id: str = "main",
    window_title: str = "Main",
    tab_index: int | None = None,
) -> list[LifecycleResult]:
    records_list = list(records)
    results: list[LifecycleResult] = []
    next_index = next_tab_index(store, window_id) if tab_index is None else max(0, int(tab_index))
    for offset, record in enumerate(records_list):
        store.update(
            record.id,
            visible=True,
            window_id=window_id or "main",
            window_title=window_title or window_id or "Main",
            tab_index=next_index + offset,
            last_opened_at=utc_now(),
        )
    compact_window(store, window_id or "main")
    for record in records_list:
        refreshed = store.get(record.id)
        if refreshed:
            results.append(LifecycleResult.from_record(refreshed, "show", detail="visible in browser restore; process state unchanged"))
    return results


def move_records(
    store: SessionStore,
    records: Iterable[SessionRecord],
    *,
    window_id: str,
    window_title: str,
    tab_index: int | None = None,
    visible: bool = True,
) -> list[LifecycleResult]:
    records_list = list(records)
    old_windows = {record.window_id for record in records_list}
    new_window = window_id or "main"
    start_index = next_tab_index(store, new_window) if tab_index is None else max(0, int(tab_index))
    results: list[LifecycleResult] = []
    for offset, record in enumerate(records_list):
        store.update(
            record.id,
            window_id=new_window,
            window_title=window_title or new_window,
            tab_index=start_index + offset,
            visible=visible,
            last_opened_at=utc_now(),
        )
    for old_window in old_windows | {new_window}:
        compact_window(store, old_window)
    for record in records_list:
        refreshed = store.get(record.id)
        if refreshed:
            results.append(LifecycleResult.from_record(refreshed, "move", detail="moved to another browser window/tab slot"))
    return results


def rename_record(store: SessionStore, record: SessionRecord, new_name: str) -> LifecycleResult:
    clean = new_name.strip()
    if not clean:
        return LifecycleResult.from_record(record, "rename", ok=False, detail="new name is empty")
    updated = store.update(record.id, name=clean, last_opened_at=utc_now())
    return LifecycleResult.from_record(updated, "rename", detail="renamed browser tab; process state unchanged")


def retag_records(store: SessionStore, records: Iterable[SessionRecord], new_tag: str) -> list[LifecycleResult]:
    clean = new_tag.strip() or "default"
    results: list[LifecycleResult] = []
    for record in records:
        updated = store.update(record.id, tag=clean, last_opened_at=utc_now())
        results.append(LifecycleResult.from_record(updated, "retag", detail="changed workspace/tag; process state unchanged"))
    return results


def pin_records(store: SessionStore, records: Iterable[SessionRecord]) -> list[LifecycleResult]:
    """Pin browser tabs so they stay at the front and resist cleanup actions."""

    records_list = list(records)
    touched_windows = {record.window_id for record in records_list}
    results: list[LifecycleResult] = []
    for record in records_list:
        updated = store.update(record.id, pinned=True, visible=True, last_opened_at=utc_now())
        results.append(LifecycleResult.from_record(updated, "pin", detail="pinned tab and kept it visible"))
    for window_id in touched_windows:
        compact_window(store, window_id)
    return [LifecycleResult.from_record(store.get(result.id) or next(r for r in records_list if r.id == result.id), "pin", detail=result.detail) for result in results]


def unpin_records(store: SessionStore, records: Iterable[SessionRecord]) -> list[LifecycleResult]:
    """Remove pinned protection while preserving process state."""

    records_list = list(records)
    touched_windows = {record.window_id for record in records_list}
    results: list[LifecycleResult] = []
    for record in records_list:
        updated = store.update(record.id, pinned=False, last_opened_at=utc_now())
        results.append(LifecycleResult.from_record(updated, "unpin", detail="unpinned tab; process state unchanged"))
    for window_id in touched_windows:
        compact_window(store, window_id)
    return [LifecycleResult.from_record(store.get(result.id) or next(r for r in records_list if r.id == result.id), "unpin", detail=result.detail) for result in results]


def archive_records(store: SessionStore, records: Iterable[SessionRecord], *, include_pinned: bool = False) -> list[LifecycleResult]:
    """Remove sessions from normal browser views without killing their processes."""

    results: list[LifecycleResult] = []
    touched_windows: set[str] = set()
    for record in records:
        if record.pinned and not include_pinned:
            results.append(_protected_result(record, "archive"))
            continue
        updated = store.update(record.id, status="archived", visible=False, last_opened_at=utc_now())
        touched_windows.add(record.window_id)
        results.append(LifecycleResult.from_record(updated, "archive", detail="archived in registry; process state unchanged"))
    for window_id in touched_windows:
        compact_window(store, window_id)
    return results


def recently_closed_records(
    store: SessionStore,
    *,
    limit: int = 20,
    tag: str | None = None,
) -> list[SessionRecord]:
    """Return recently hidden tabs that can be reopened without restart."""

    return store.hidden_records(limit=limit, tag=tag, include_archived=False, include_stopped=False)


def reopen_closed_records(
    store: SessionStore,
    records: Iterable[SessionRecord],
    *,
    window_id: str | None = None,
    window_title: str | None = None,
) -> list[LifecycleResult]:
    """Reopen hidden tabs without restarting or signalling terminal tasks."""

    candidates = [record for record in records if not record.visible and record.status not in {"archived", "stopped"}]
    if not candidates:
        return []

    grouped: dict[tuple[str, str], list[SessionRecord]] = {}
    for record in candidates:
        target_window = window_id or record.window_id or "main"
        target_title = window_title or record.window_title or target_window or "Main"
        grouped.setdefault((target_window, target_title), []).append(record)

    results: list[LifecycleResult] = []
    for (target_window, target_title), rows in grouped.items():
        start_index = next_tab_index(store, target_window)
        # Reopen the most recently closed tab first while preserving stable order
        # for multi-tab reopen operations.
        for offset, record in enumerate(reversed(rows)):
            store.update(
                record.id,
                visible=True,
                window_id=target_window,
                window_title=target_title,
                tab_index=start_index + offset,
                last_opened_at=utc_now(),
            )
        compact_window(store, target_window)
        for record in reversed(rows):
            refreshed = store.get(record.id)
            if refreshed:
                results.append(
                    LifecycleResult.from_record(
                        refreshed,
                        "reopen-closed",
                        detail="reopened hidden browser tab; process state unchanged",
                    )
                )
    return results
