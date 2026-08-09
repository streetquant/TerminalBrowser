from __future__ import annotations

from dataclasses import dataclass

from .health import HealthPolicy, evaluate_health, health_badge
from .models import ResourceSnapshot, SessionRecord
from .resources import format_bytes


@dataclass(frozen=True)
class MasterTaskRow:
    """Presentation row for the browser-like master task window.

    Keeping this logic outside GTK makes filtering/status formatting testable
    without a graphical runtime.
    """

    session_id: str
    window_title: str
    name: str
    tag: str
    pin: str
    pid: str
    state: str
    health: str
    cpu: str
    ram: str
    process_count: str
    runtime: str
    focus_key: str
    pause_key: str
    command: str
    cwd: str

    @property
    def search_text(self) -> str:
        return " ".join(
            [
                self.session_id,
                self.window_title,
                self.name,
                self.tag,
                self.pin,
                "pinned" if self.pin else "unpinned",
                self.pid,
                self.state,
                self.health,
                self.command,
                self.cwd,
            ]
        ).lower()

    def as_list(self) -> list[str]:
        return [
            self.session_id,
            self.window_title,
            self.name,
            self.tag,
            self.pin,
            self.pid,
            self.state,
            self.health,
            self.cpu,
            self.ram,
            self.process_count,
            self.runtime,
            self.focus_key,
            self.pause_key,
            self.command,
            self.cwd,
        ]


def state_from_snapshot(record: SessionRecord, snap: ResourceSnapshot) -> str:
    if snap.exists:
        return "paused" if snap.state == "T" or (snap.state == "unknown" and record.status == "paused") else "running"
    if record.status == "paused":
        return "paused"
    return record.status if record.status in {"new", "recovery-pending", "stopped", "archived"} else "stopped"


def row_from_record(
    record: SessionRecord,
    *,
    pid: int | None,
    snapshot: ResourceSnapshot,
    focus_key: str,
    pause_key: str,
    health: str = "",
    policy: HealthPolicy | None = None,
) -> MasterTaskRow:
    return MasterTaskRow(
        session_id=record.id,
        window_title=record.window_title,
        name=record.name,
        tag=record.tag,
        pin="★" if record.pinned else "",
        pid=str(pid or ""),
        state=state_from_snapshot(record, snapshot),
        health=health or health_badge(evaluate_health(record, snapshot, policy)),
        cpu=f"{snapshot.cpu_percent:.1f}%" if snapshot.exists else "",
        ram=format_bytes(snapshot.memory_bytes) if snapshot.exists else "",
        process_count=str(snapshot.process_count) if snapshot.exists else "",
        runtime=snapshot.runtime_label if snapshot.exists else "",
        focus_key=focus_key,
        pause_key=pause_key,
        command=record.command,
        cwd=record.cwd,
    )


def filter_rows(rows: list[MasterTaskRow], query: str) -> list[MasterTaskRow]:
    tokens = [token for token in query.lower().split() if token]
    if not tokens:
        return rows
    return [row for row in rows if all(token in row.search_text for token in tokens)]
