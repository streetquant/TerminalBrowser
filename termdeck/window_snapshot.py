from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .resources import format_bytes
from .task_snapshot import TaskSnapshotItem

WindowSortKey = Literal["health", "cpu", "ram", "tabs", "title", "running", "attention"]


@dataclass(frozen=True)
class WindowSnapshot:
    """Aggregated browser-window row for the global TermDeck overview.

    The master task table answers "which tab is hot?".  This model answers
    "which browser-like window is carrying the most terminal work?" without
    exposing tmux internals to the user.
    """

    window_id: str
    title: str
    tab_count: int
    visible_count: int
    running_count: int
    paused_count: int
    stopped_count: int
    missing_count: int
    attention_count: int
    critical_count: int
    cpu_percent: float
    memory_bytes: int
    process_count: int
    max_runtime_seconds: float
    top_health: str
    top_health_severity: int

    @property
    def memory(self) -> str:
        return format_bytes(self.memory_bytes) if self.memory_bytes else "0 B"

    @property
    def runtime(self) -> str:
        seconds = max(0, int(self.max_runtime_seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {secs:02d}s"
        return f"{secs}s"

    @property
    def state_summary(self) -> str:
        parts = []
        for label, count in (
            ("running", self.running_count),
            ("paused", self.paused_count),
            ("stopped", self.stopped_count),
            ("missing", self.missing_count),
        ):
            if count:
                parts.append(f"{count} {label}")
        return ", ".join(parts) if parts else "empty"

    @property
    def search_text(self) -> str:
        return " ".join(
            str(part)
            for part in (
                self.window_id,
                self.title,
                self.top_health,
                self.state_summary,
                self.tab_count,
                self.visible_count,
                self.attention_count,
                self.critical_count,
            )
        ).casefold()


def build_window_snapshots(items: Iterable[TaskSnapshotItem]) -> list[WindowSnapshot]:
    grouped: dict[str, list[TaskSnapshotItem]] = {}
    titles: dict[str, str] = {}
    for item in items:
        window_id = item.record.window_id or "main"
        grouped.setdefault(window_id, []).append(item)
        titles.setdefault(window_id, item.record.window_title or "Main")

    snapshots: list[WindowSnapshot] = []
    for window_id, rows in grouped.items():
        title = titles.get(window_id, "Main")
        tab_count = len(rows)
        visible_count = sum(1 for item in rows if item.record.visible)
        running_count = sum(1 for item in rows if item.row.state == "running")
        paused_count = sum(1 for item in rows if item.row.state == "paused")
        stopped_count = sum(1 for item in rows if item.row.state == "stopped")
        missing_count = sum(1 for item in rows if item.health.label == "missing")
        attention_count = sum(1 for item in rows if item.health.severity >= 2)
        critical_count = sum(1 for item in rows if item.health.severity >= 4)
        cpu_percent = sum(item.snapshot.cpu_percent for item in rows if item.snapshot.exists)
        memory_bytes = sum(item.snapshot.memory_bytes for item in rows if item.snapshot.exists)
        process_count = sum(item.snapshot.process_count for item in rows if item.snapshot.exists)
        max_runtime_seconds = max((item.snapshot.runtime_seconds for item in rows if item.snapshot.exists), default=0.0)
        top = max(rows, key=lambda item: (item.health.severity, item.snapshot.cpu_percent, item.snapshot.memory_bytes))
        snapshots.append(
            WindowSnapshot(
                window_id=window_id,
                title=title,
                tab_count=tab_count,
                visible_count=visible_count,
                running_count=running_count,
                paused_count=paused_count,
                stopped_count=stopped_count,
                missing_count=missing_count,
                attention_count=attention_count,
                critical_count=critical_count,
                cpu_percent=cpu_percent,
                memory_bytes=memory_bytes,
                process_count=process_count,
                max_runtime_seconds=max_runtime_seconds,
                top_health=top.health.label,
                top_health_severity=top.health.severity,
            )
        )
    return sorted(snapshots, key=lambda row: (row.title.casefold(), row.window_id))


def filter_window_snapshots(rows: Iterable[WindowSnapshot], query: str) -> list[WindowSnapshot]:
    row_list = list(rows)
    terms = [term for term in query.casefold().split() if term]
    if not terms:
        return row_list
    return [row for row in row_list if all(term in row.search_text for term in terms)]


def sort_window_snapshots(rows: Iterable[WindowSnapshot], key: str = "health") -> list[WindowSnapshot]:
    row_list = list(rows)
    if key == "cpu":
        return sorted(row_list, key=lambda row: (-row.cpu_percent, row.title.casefold()))
    if key in {"ram", "memory"}:
        return sorted(row_list, key=lambda row: (-row.memory_bytes, row.title.casefold()))
    if key == "tabs":
        return sorted(row_list, key=lambda row: (-row.tab_count, row.title.casefold()))
    if key == "running":
        return sorted(row_list, key=lambda row: (-row.running_count, row.title.casefold()))
    if key == "attention":
        return sorted(row_list, key=lambda row: (-row.attention_count, -row.critical_count, row.title.casefold()))
    if key == "title":
        return sorted(row_list, key=lambda row: (row.title.casefold(), row.window_id))
    return sorted(row_list, key=lambda row: (-row.top_health_severity, -row.critical_count, -row.attention_count, row.title.casefold()))


def window_snapshots_to_dicts(rows: Iterable[WindowSnapshot]) -> list[dict[str, object]]:
    return [
        {
            "window_id": row.window_id,
            "title": row.title,
            "tab_count": row.tab_count,
            "visible_count": row.visible_count,
            "running_count": row.running_count,
            "paused_count": row.paused_count,
            "stopped_count": row.stopped_count,
            "missing_count": row.missing_count,
            "attention_count": row.attention_count,
            "critical_count": row.critical_count,
            "cpu_percent": round(row.cpu_percent, 1),
            "memory_bytes": row.memory_bytes,
            "memory": row.memory,
            "process_count": row.process_count,
            "max_runtime_seconds": round(row.max_runtime_seconds, 1),
            "runtime": row.runtime,
            "top_health": row.top_health,
            "top_health_severity": row.top_health_severity,
            "state_summary": row.state_summary,
        }
        for row in rows
    ]


def render_window_table(rows: Iterable[WindowSnapshot]) -> str:
    row_list = list(rows)
    if not row_list:
        return "No remembered windows."
    headers = ["Window", "Tabs", "Visible", "State", "Hot", "Critical", "CPU", "RAM", "Procs", "Runtime", "Top health"]
    values = [
        [
            row.title,
            str(row.tab_count),
            str(row.visible_count),
            row.state_summary,
            str(row.attention_count),
            str(row.critical_count),
            f"{row.cpu_percent:.1f}%",
            row.memory,
            str(row.process_count),
            row.runtime,
            row.top_health,
        ]
        for row in row_list
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
