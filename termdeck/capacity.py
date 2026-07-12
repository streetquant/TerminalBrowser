from __future__ import annotations

from collections.abc import Iterable

from .models import SessionRecord


# These are control-plane limits, not limits on running tmux sessions. Background
# sessions stay alive; the GTK process only materialises a bounded working set.
STARTUP_WINDOW_LIMIT = 4
STARTUP_TABS_PER_WINDOW = 12
STARTUP_TERMINAL_LIMIT = 24
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
    """Choose the terminals GTK should attach without hiding other sessions.

    Pinned and recently used windows/tabs win the bounded working set. Records
    omitted here remain visible in the registry and alive in tmux, so search or
    the Tab manager can open them later without a restart.
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

    selected_groups: list[WindowGroup] = []
    for _index, (window_id, window_title, records) in candidates[:clean_window_limit]:
        if remaining <= 0:
            break
        take = min(clean_tabs_per_window, remaining)
        ranked = sorted(
            records,
            key=lambda record: (record.pinned, record.last_opened_at, -record.tab_index),
            reverse=True,
        )[:take]
        ranked.sort(key=lambda record: (record.tab_index, record.last_opened_at, record.id))
        if ranked:
            selected_groups.append((window_id, window_title, ranked))
            remaining -= len(ranked)
    return selected_groups
