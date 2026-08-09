from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Literal

from .health import HealthPolicy, SessionHealth, evaluate_health, health_badge
from .liveness import merge_confirmed_pane_snapshot
from .master_tasks import MasterTaskRow, filter_rows, row_from_record
from .models import ResourceSnapshot, SessionRecord
from .resources import ResourceSampler
from .shortcuts import tab_shortcut
from .tmux_backend import TmuxBackend, TmuxError

SortKey = Literal["health", "cpu", "ram", "runtime", "name", "tag", "window", "tab", "status", "pinned"]
PolicyResolver = Callable[[SessionRecord], HealthPolicy]


@dataclass(frozen=True)
class TaskSnapshotItem:
    """One master-task-manager row plus raw values for sorting/automation."""

    record: SessionRecord
    pid: int | None
    snapshot: ResourceSnapshot
    health: SessionHealth
    row: MasterTaskRow

    @property
    def sort_health(self) -> tuple[int, str, str]:
        return (-self.health.severity, not self.record.pinned, self.record.window_title.casefold(), self.record.name.casefold())


def _live_pid(record: SessionRecord, backend: TmuxBackend) -> tuple[int | None, bool]:
    """Return the best PID plus whether a fresh tmux pane confirmed it."""

    if not backend.available:
        return record.last_pid, False
    try:
        pid = backend.pane_pid(record)
    except TmuxError:
        return record.last_pid, False
    if getattr(backend, "pane_snapshot_stale", False):
        return (pid if pid is not None else record.last_pid), False
    return pid, pid is not None


def build_task_snapshot(
    records: Iterable[SessionRecord],
    *,
    backend: TmuxBackend | None = None,
    sampler: ResourceSampler | None = None,
    policy: HealthPolicy | None = None,
    policy_resolver: Callable[[SessionRecord], HealthPolicy] | None = None,
    snapshot_refresh_limit: int | None = None,
) -> list[TaskSnapshotItem]:
    """Build testable browser-task-manager rows for remembered sessions.

    ``policy`` applies one budget to all rows. ``policy_resolver`` allows
    per-tag budgets from the local TermDeck config and takes precedence.
    """

    backend = backend or TmuxBackend()
    sampler = sampler or ResourceSampler()
    record_list = list(records)
    # Batch tmux pane resolution (one list-panes -a) instead of 2N spawns.
    batch_pids: dict[str, int | None] = {}
    if backend.available and hasattr(backend, "pane_pids"):
        batch_pids = backend.pane_pids(record_list)

    pane_snapshot_stale = bool(getattr(backend, "pane_snapshot_stale", False))
    resolved_pids: list[int | None] = []
    confirmed_panes: list[bool] = []
    for record in record_list:
        if record.id in batch_pids:
            pane_pid = batch_pids[record.id]
            if pane_snapshot_stale:
                pid = pane_pid if pane_pid is not None else record.last_pid
                confirmed = False
            else:
                # A successful all-pane snapshot is authoritative. Do not let a
                # remembered/reused PID revive an absent or dead tmux pane.
                pid = pane_pid
                confirmed = pane_pid is not None
        else:
            pid, confirmed = _live_pid(record, backend)
        resolved_pids.append(pid)
        confirmed_panes.append(confirmed)

    sample_many = getattr(sampler, "snapshots", None)
    if pane_snapshot_stale:
        snapshots = [
            ResourceSnapshot(
                pid=pid,
                exists=record.status.casefold() in {"running", "paused", "missing"},
                state="unknown",
            )
            for record, pid in zip(record_list, resolved_pids)
        ]
    elif callable(sample_many):
        if snapshot_refresh_limit is None:
            snapshots = list(sample_many(resolved_pids))
        else:
            try:
                snapshots = list(sample_many(resolved_pids, refresh_limit=snapshot_refresh_limit))
            except TypeError:
                snapshots = [sampler.snapshot(pid) for pid in resolved_pids]
    else:
        snapshots = [sampler.snapshot(pid) for pid in resolved_pids]

    if not pane_snapshot_stale:
        snapshots = [
            merge_confirmed_pane_snapshot(
                snapshot,
                pane_pid=pid,
                pane_confirmed=confirmed,
            )
            for snapshot, pid, confirmed in zip(snapshots, resolved_pids, confirmed_panes)
        ]

    items: list[TaskSnapshotItem] = []
    for record, pid, snapshot in zip(record_list, resolved_pids, snapshots):
        row_policy = policy_resolver(record) if policy_resolver else policy
        health = evaluate_health(record, snapshot, row_policy)
        shortcut = tab_shortcut(record.tab_index + 1) if record.visible else tab_shortcut(0)
        row = row_from_record(
            record,
            pid=pid,
            snapshot=snapshot,
            focus_key=shortcut.focus,
            pause_key=shortcut.pause_toggle,
            health=health_badge(health),
            policy=row_policy,
        )
        items.append(TaskSnapshotItem(record=record, pid=pid, snapshot=snapshot, health=health, row=row))
    return items


def filter_task_snapshot(items: Iterable[TaskSnapshotItem], query: str) -> list[TaskSnapshotItem]:
    item_list = list(items)
    rows = filter_rows([item.row for item in item_list], query)
    allowed = {row.session_id for row in rows}
    return [item for item in item_list if item.row.session_id in allowed]


def sort_task_snapshot(items: Iterable[TaskSnapshotItem], key: str = "health") -> list[TaskSnapshotItem]:
    item_list = list(items)
    if key == "cpu":
        return sorted(item_list, key=lambda item: (-item.snapshot.cpu_percent, item.record.name.casefold()))
    if key in {"ram", "memory"}:
        return sorted(item_list, key=lambda item: (-item.snapshot.memory_bytes, item.record.name.casefold()))
    if key == "runtime":
        return sorted(item_list, key=lambda item: (-item.snapshot.runtime_seconds, item.record.name.casefold()))
    if key == "name":
        return sorted(item_list, key=lambda item: (item.record.name.casefold(), item.record.tag.casefold()))
    if key == "tag":
        return sorted(item_list, key=lambda item: (item.record.tag.casefold(), item.record.name.casefold()))
    if key == "window":
        return sorted(item_list, key=lambda item: (item.record.window_title.casefold(), item.record.tab_index, item.record.name.casefold()))
    if key == "tab":
        return sorted(item_list, key=lambda item: (item.record.tab_index, item.record.window_title.casefold(), item.record.name.casefold()))
    if key == "status":
        return sorted(item_list, key=lambda item: (item.row.state.casefold(), item.record.name.casefold()))
    if key in {"pinned", "pin"}:
        return sorted(item_list, key=lambda item: (not item.record.pinned, item.record.window_title.casefold(), item.record.tab_index, item.record.name.casefold()))
    return sorted(item_list, key=lambda item: item.sort_health)


def task_snapshot_to_dicts(items: Iterable[TaskSnapshotItem]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items:
        row = item.row
        rows.append(
            {
                "id": row.session_id,
                "window": row.window_title,
                "name": row.name,
                "tag": row.tag,
                "pinned": item.record.pinned,
                "pid": item.pid,
                "state": row.state,
                "health": item.health.label,
                "health_level": item.health.level,
                "health_severity": item.health.severity,
                "health_badge": row.health,
                "reason": item.health.reason,
                "suggested_action": item.health.suggested_action,
                "cpu_percent": round(item.snapshot.cpu_percent, 1) if item.snapshot.exists else 0.0,
                "memory_bytes": item.snapshot.memory_bytes if item.snapshot.exists else 0,
                "memory": row.ram,
                "process_count": item.snapshot.process_count if item.snapshot.exists else 0,
                "runtime": row.runtime,
                "runtime_seconds": round(item.snapshot.runtime_seconds, 1) if item.snapshot.exists else 0.0,
                "focus_key": row.focus_key,
                "pause_key": row.pause_key,
                "command": row.command,
                "cwd": row.cwd,
                "visible": item.record.visible,
                "tab_index": item.record.tab_index,
                "tmux_name": item.record.tmux_name,
            }
        )
    return rows


def render_task_table(items: Iterable[TaskSnapshotItem]) -> str:
    """Render a compact terminal-friendly master task table."""

    rows = [item.row for item in items]
    if not rows:
        return "No remembered sessions."
    headers = ["ID", "Window", "Name", "Tag", "Pin", "PID", "State", "Health", "CPU", "RAM", "Focus", "Pause"]
    values = [
        [
            row.session_id,
            row.window_title,
            row.name,
            row.tag,
            row.pin or "-",
            row.pid or "-",
            row.state,
            row.health,
            row.cpu or "-",
            row.ram or "-",
            row.focus_key or "-",
            row.pause_key or "-",
        ]
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for value_row in values:
        for index, value in enumerate(value_row):
            widths[index] = min(32, max(widths[index], len(value)))

    def clean(value: str, width: int) -> str:
        if len(value) > width:
            return value[: max(1, width - 1)] + "…"
        return value.ljust(width)

    lines = ["  ".join(clean(header, widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for value_row in values:
        lines.append("  ".join(clean(value, widths[index]) for index, value in enumerate(value_row)))
    return "\n".join(lines)
