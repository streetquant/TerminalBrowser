from __future__ import annotations

from collections.abc import Iterable

from .models import SessionRecord


# Explicit callers may still request a bounded attachment plan. The GUI startup
# path deliberately does not use these limits: it restores every visible tab.
STARTUP_TABS_PER_WINDOW = 12
STARTUP_TERMINAL_LIMIT = 24
STARTUP_WINDOW_LIMIT = STARTUP_TERMINAL_LIMIT
UI_RESOURCE_REFRESH_BUDGET = 128
SIDE_PANE_ROW_LIMIT = 100
MASTER_ROW_LIMIT = 500


WindowGroup = tuple[str, str, list[SessionRecord]]


def bounded_registry_records(
    records: Iterable[SessionRecord],
    *,
    limit: int,
    preferred_ids: Iterable[str] = (),
) -> list[SessionRecord]:
    """Materialise a bounded, stable registry view.

    Explicitly selected rows, pinned sessions, visible sessions, and recent
    activity determine membership. The returned rows retain registry order so
    the side pane does not jump around between refreshes.
    """

    candidates = list(records)
    clean_limit = max(1, int(limit))
    if len(candidates) <= clean_limit:
        return candidates
    preferred = set(preferred_ids)
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            item[1].id in preferred,
            item[1].pinned,
            item[1].visible,
            item[1].last_opened_at,
            -item[0],
        ),
        reverse=True,
    )[:clean_limit]
    chosen = {record.id for _index, record in ranked}
    return [record for record in candidates if record.id in chosen]


def bounded_startup_groups(
    groups: Iterable[WindowGroup],
    *,
    window_limit: int = STARTUP_WINDOW_LIMIT,
    tabs_per_window: int = STARTUP_TABS_PER_WINDOW,
    terminal_limit: int = STARTUP_TERMINAL_LIMIT,
) -> list[WindowGroup]:
    """Choose bounded terminal attachments while retaining every window shell.

    Pinned and recently used windows/tabs win the bounded working set. Every
    remembered window remains in the result, including windows that receive no
    initial attachment. Within the global attachment budget, reserve one slot
    per window before filling extra tabs so a large window cannot starve a
    smaller one.
    """

    clean_window_limit = max(1, int(window_limit))
    clean_tabs_per_window = max(1, int(tabs_per_window))
    remaining = max(1, int(terminal_limit))
    candidates = [(index, group) for index, group in enumerate(groups) if group[2]]
    candidates.sort(
        key=lambda item: (
            any(record.pinned for record in item[1][2]),
            max((record.last_opened_at for record in item[1][2]), default=""),
            -item[0],
        ),
        reverse=True,
    )

    ranked_candidates: list[tuple[int, str, str, list[SessionRecord]]] = []
    for index, (window_id, window_title, records) in candidates:
        ranked = sorted(
            records,
            key=lambda record: (record.pinned, record.last_opened_at, -record.tab_index),
            reverse=True,
        )
        ranked_candidates.append((index, window_id, window_title, ranked))

    attachable = ranked_candidates[:clean_window_limit]
    selected: dict[int, list[SessionRecord]] = {index: [] for index, *_rest in ranked_candidates}

    for index, _window_id, _window_title, ranked in attachable:
        if remaining <= 0:
            break
        selected[index].append(ranked[0])
        remaining -= 1

    for index, _window_id, _window_title, ranked in attachable:
        chosen = selected[index]
        take = min(clean_tabs_per_window - len(chosen), remaining)
        if take <= 0:
            continue
        chosen.extend(ranked[len(chosen) : len(chosen) + take])
        remaining -= take

    restored_groups: list[WindowGroup] = []
    for index, window_id, window_title, _ranked in ranked_candidates:
        records = sorted(
            selected[index],
            key=lambda record: (record.tab_index, record.last_opened_at, record.id),
        )
        restored_groups.append((window_id, window_title, records))
    return restored_groups
