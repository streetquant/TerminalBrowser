from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from .health import HealthPolicy, evaluate_health, health_badge
from .models import ResourceSnapshot, SessionRecord


PidResolver = Callable[[SessionRecord], int | None]
SnapshotResolver = Callable[[int | None], ResourceSnapshot]

_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive delete", re.compile(r"\brm\s+(?=[^;&|]*-[A-Za-z]*[rR][A-Za-z]*)(?=[^;&|]*-[A-Za-z]*[fF][A-Za-z]*|[^;&|]*-[A-Za-z]*[rR][fF][A-Za-z]*|[^;&|]*-[A-Za-z]*[fF][rR][A-Za-z]*).*", re.I)),
    ("force clean", re.compile(r"\bgit\s+clean\s+-[A-Za-z]*[fdx]")),
    ("hard reset", re.compile(r"\bgit\s+reset\s+--hard\b")),
    ("database destructive statement", re.compile(r"\b(drop\s+table|drop\s+database|delete\s+from|truncate\s+table)\b", re.I)),
    ("container prune", re.compile(r"\bdocker\s+(?:system|volume|image|container)\s+prune\b")),
    ("kubernetes delete", re.compile(r"\bkubectl\s+delete\b")),
    ("terraform destroy", re.compile(r"\bterraform\s+destroy\b")),
    ("disk overwrite", re.compile(r"\bdd\b.*\bof=", re.I)),
    ("filesystem creation", re.compile(r"\bmkfs(?:\.[A-Za-z0-9_+-]+)?\b")),
    ("system power command", re.compile(r"\b(reboot|shutdown|poweroff|halt)\b")),
    ("process kill", re.compile(r"\bkill(?:all)?\b")),
)


def destructive_hints(text: str) -> list[str]:
    """Return matched destructive-command hints for preview UI and CLI plans."""

    clean = text.strip()
    if not clean:
        return []
    hints = []
    for label, pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(clean):
            hints.append(label)
    return hints


def preview_text(text: str, limit: int = 120) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)] + "…"


@dataclass(frozen=True)
class ControlPlanRow:
    id: str
    name: str
    tag: str
    window: str
    action: str
    status: str
    pinned: bool
    pid: int | None
    health: str
    risk: str
    requires_confirmation: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "window": self.window,
            "action": self.action,
            "status": self.status,
            "pinned": self.pinned,
            "pid": self.pid,
            "health": self.health,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ControlPlan:
    action: str
    count: int
    risk: str
    requires_confirmation: bool
    selection: str
    text_preview: str = ""
    enter: bool = False
    destructive_hints: tuple[str, ...] = ()
    rows: tuple[ControlPlanRow, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "count": self.count,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "selection": self.selection,
            "text_preview": self.text_preview,
            "enter": self.enter,
            "destructive_hints": list(self.destructive_hints),
            "sessions": [row.as_dict() for row in self.rows],
        }


_RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def max_risk(labels: Iterable[str]) -> str:
    result = "low"
    for label in labels:
        if _RISK_ORDER.get(label, 0) > _RISK_ORDER[result]:
            result = label
    return result


def selection_label(*, selectors: Iterable[str] | None = None, tag: str | None = None, all_sessions: bool = False) -> str:
    selectors = [item for item in (selectors or []) if item]
    parts = []
    if all_sessions:
        parts.append("all sessions")
    if tag:
        parts.append(f"tag:{tag}")
    if selectors:
        parts.append(",".join(selectors))
    return " + ".join(parts) if parts else "explicit selection"


def _row_risk(action: str, *, count: int, enter: bool, hints: list[str], record: SessionRecord) -> tuple[str, bool, str]:
    if action == "kill":
        if record.pinned:
            return "critical", True, "pinned tab is protected from accidental stop; use --include-pinned only when intentional"
        return "high", True, "stops the terminal task and hides it from restore"
    if action == "send":
        if hints:
            return "critical", True, "text looks destructive: " + ", ".join(hints)
        if enter and count > 1:
            return "high", True, "submits the same input to multiple sessions"
        if enter:
            return "medium", False, "presses Enter in the target session"
        return "low", False, "types text without pressing Enter"
    if action == "pause":
        if record.status == "paused":
            return "low", False, "session is already marked paused"
        if count > 3:
            return "medium", True, "freezes several process groups"
        return "medium", False, "freezes the process group until resumed"
    if action == "resume":
        return "low", False, "continues a stopped process group"
    if action == "pause-hot":
        if record.pinned:
            return "high", True, "pinned tab is protected from automatic resource pausing"
        return "medium", count > 1, "freezes sessions selected by resource pressure"
    if action == "pause-over-budget":
        if record.pinned:
            return "high", True, "pinned tab is protected from budget automation"
        return "medium", True, "freezes sessions whose budget action is pause"
    if action == "capture":
        return "low", False, "reads recent terminal output only"
    return "medium", True, "unknown control action"


def build_control_plan(
    records: list[SessionRecord],
    action: str,
    *,
    text: str = "",
    enter: bool = False,
    selectors: Iterable[str] | None = None,
    tag: str | None = None,
    all_sessions: bool = False,
    pid_resolver: PidResolver | None = None,
    snapshot_resolver: SnapshotResolver | None = None,
    policy: HealthPolicy | None = None,
) -> ControlPlan:
    hints = destructive_hints(text) if action == "send" else []
    rows: list[ControlPlanRow] = []
    for record in records:
        pid = pid_resolver(record) if pid_resolver else record.last_pid
        snapshot = snapshot_resolver(pid) if snapshot_resolver else ResourceSnapshot(pid=pid, exists=False)
        health = health_badge(evaluate_health(record, snapshot, policy))
        risk, confirm, reason = _row_risk(action, count=len(records), enter=enter, hints=hints, record=record)
        rows.append(
            ControlPlanRow(
                id=record.id,
                name=record.name,
                tag=record.tag,
                window=record.window_title,
                action=action,
                status=record.status,
                pinned=record.pinned,
                pid=pid,
                health=health,
                risk=risk,
                requires_confirmation=confirm,
                reason=reason,
            )
        )

    aggregate = max_risk(row.risk for row in rows) if rows else "low"
    requires = any(row.requires_confirmation for row in rows)
    if all_sessions and action in {"pause", "resume", "kill", "send", "pause-hot", "pause-over-budget"} and records:
        requires = True
        aggregate = max_risk([aggregate, "medium"])
    if action == "send" and not text.strip():
        requires = True
        aggregate = max_risk([aggregate, "medium"])

    return ControlPlan(
        action=action,
        count=len(records),
        risk=aggregate,
        requires_confirmation=requires,
        selection=selection_label(selectors=selectors, tag=tag, all_sessions=all_sessions),
        text_preview=preview_text(text) if action == "send" else "",
        enter=enter if action == "send" else False,
        destructive_hints=tuple(hints),
        rows=tuple(rows),
    )


def render_control_plan_text(plan: ControlPlan, *, max_rows: int = 8) -> str:
    """Render a compact human-readable summary for GUI confirmations.

    The JSON plan is better for automation, but the GTK side pane and master
    task manager need a short explanation that a browser user can understand
    before a bulk or destructive action is executed.
    """

    lines = [
        f"Action: {plan.action}",
        f"Selection: {plan.selection}",
        f"Sessions: {plan.count}",
        f"Risk: {plan.risk}",
    ]
    if plan.action == "send":
        lines.append(f"Send mode: {'type text and press Enter' if plan.enter else 'type text only'}")
        if plan.text_preview:
            lines.append(f"Text: {plan.text_preview}")
        if plan.destructive_hints:
            lines.append("Destructive hints: " + ", ".join(plan.destructive_hints))
    if plan.requires_confirmation:
        lines.append("Confirmation required before t1000 touches the selected sessions.")
    if not plan.rows:
        lines.append("No matching sessions.")
        return "\n".join(lines)

    lines.append("")
    lines.append("Target sessions:")
    for row in plan.rows[:max_rows]:
        lines.append(
            f"- {row.id} [{row.tag}] {'★ ' if row.pinned else ''}{row.name}: {row.health}; {row.reason}"
        )
    remaining = len(plan.rows) - max_rows
    if remaining > 0:
        lines.append(f"- ... and {remaining} more session(s)")
    return "\n".join(lines)


def render_control_plan_lines(plan: ControlPlan, *, row_limit: int = 8) -> list[str]:
    """Render a concise, user-facing confirmation summary.

    The GTK app uses this text in bulk-control dialogs, and tests exercise it
    without importing GTK. Keeping the summary here prevents GUI controls from
    silently drifting away from the CLI safety model.
    """

    lines = [
        f"Action: {plan.action}",
        f"Risk: {plan.risk}",
        f"Sessions: {plan.count}",
    ]
    if plan.text_preview:
        lines.append(f"Text preview: {plan.text_preview}")
    if plan.destructive_hints:
        lines.append("Destructive-looking input: " + ", ".join(plan.destructive_hints))
    for row in plan.rows[:row_limit]:
        lines.append(f"• {row.id} [{row.tag}] {'★ ' if row.pinned else ''}{row.name} — {row.health}; {row.reason}")
    remaining = len(plan.rows) - row_limit
    if remaining > 0:
        lines.append(f"• …and {remaining} more session(s)")
    return lines


def render_control_plan_text(plan: ControlPlan, *, row_limit: int = 8) -> str:
    return "\n".join(render_control_plan_lines(plan, row_limit=row_limit))
