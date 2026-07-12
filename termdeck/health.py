from __future__ import annotations

from dataclasses import dataclass

from .models import ResourceSnapshot, SessionRecord
from .resources import format_bytes


@dataclass(frozen=True)
class HealthPolicy:
    """Resource thresholds for browser-like terminal task health.

    The defaults are conservative and never auto-stop work.  They only classify
    sessions so the master task window can surface tabs that deserve attention.
    """

    high_cpu_percent: float = 150.0
    critical_cpu_percent: float = 300.0
    high_memory_bytes: int = 2 * 1024 * 1024 * 1024
    critical_memory_bytes: int = 4 * 1024 * 1024 * 1024
    process_count_warn: int = 48

    @property
    def cpu_warn_percent(self) -> float:
        return self.high_cpu_percent

    @property
    def cpu_critical_percent(self) -> float:
        return self.critical_cpu_percent

    @property
    def memory_warn_bytes(self) -> int:
        return self.high_memory_bytes

    @property
    def memory_critical_bytes(self) -> int:
        return self.critical_memory_bytes


@dataclass(frozen=True)
class SessionHealth:
    label: str
    severity: int
    level: str
    hint: str
    reason: str = ""
    suggested_action: str = ""

    @property
    def is_attention(self) -> bool:
        return self.severity >= 2

    @property
    def sort_rank(self) -> int:
        ranks = {
            "red": 0,
            "orange": 1,
            "yellow": 2,
            "grey": 3,
            "blue": 4,
            "green": 5,
        }
        return ranks.get(self.level, 9)


def _with_extra_hint(base: str, extras: list[str]) -> str:
    if not extras:
        return base
    return f"{base}; also {', '.join(extras)}"


def evaluate_health(
    record: SessionRecord,
    snapshot: ResourceSnapshot,
    policy: HealthPolicy | None = None,
) -> SessionHealth:
    """Classify one terminal task for the master task window and CLI.

    Severity scale: 0 OK, 1 benign/non-running state, 2 watch, 3 high, 4 critical.
    """

    policy = policy or HealthPolicy()
    status = record.status.casefold()

    if status in {"stopped", "archived"}:
        return SessionHealth(
            label="stopped",
            severity=2,
            level="red",
            hint="session is stopped",
            reason="session is stopped",
            suggested_action="restart or archive",
        )

    if not snapshot.exists:
        if status == "new":
            return SessionHealth(
                label="not started",
                severity=1,
                level="grey",
                hint="no live process has been observed",
                reason="no live process has been observed",
                suggested_action="open session",
            )
        return SessionHealth(
            label="missing",
            severity=3,
            level="red",
            hint="no live process was found for the remembered session",
            reason="no live process was found for the remembered session",
            suggested_action="restart or hide",
        )

    if snapshot.state == "T" or status == "paused":
        return SessionHealth(
            label="paused",
            severity=1,
            level="grey",
            hint="process tree is stopped",
            reason="process tree is stopped",
            suggested_action="resume when needed",
        )

    if snapshot.state == "unknown" and snapshot.process_count == 0:
        return SessionHealth(
            label="sampling",
            severity=1,
            level="blue",
            hint="live pane is waiting for its bounded resource-sampling turn",
            reason="resource sample pending",
            suggested_action="wait for the next refresh",
        )

    extra: list[str] = []
    if snapshot.cpu_percent >= policy.high_cpu_percent:
        extra.append(f"high CPU {snapshot.cpu_percent:.1f}%")
    if snapshot.memory_bytes >= policy.high_memory_bytes:
        extra.append(f"high memory {format_bytes(snapshot.memory_bytes)}")
    if snapshot.process_count >= policy.process_count_warn:
        extra.append(f"many processes {snapshot.process_count}")

    if snapshot.memory_bytes >= policy.critical_memory_bytes:
        hint = _with_extra_hint(f"memory is {format_bytes(snapshot.memory_bytes)}", [x for x in extra if not x.startswith("high memory")])
        return SessionHealth(
            label="critical memory",
            severity=4,
            level="orange",
            hint=hint,
            reason=hint,
            suggested_action="inspect or pause",
        )

    if snapshot.cpu_percent >= policy.critical_cpu_percent:
        hint = _with_extra_hint(f"CPU is {snapshot.cpu_percent:.1f}%", [x for x in extra if not x.startswith("high CPU")])
        return SessionHealth(
            label="critical CPU",
            severity=4,
            level="orange",
            hint=hint,
            reason=hint,
            suggested_action="inspect or pause",
        )

    if snapshot.memory_bytes >= policy.high_memory_bytes:
        hint = f"memory is {format_bytes(snapshot.memory_bytes)}"
        return SessionHealth(
            label="high memory",
            severity=3,
            level="yellow",
            hint=hint,
            reason=hint,
            suggested_action="monitor",
        )

    if snapshot.cpu_percent >= policy.high_cpu_percent:
        hint = f"CPU is {snapshot.cpu_percent:.1f}%"
        return SessionHealth(
            label="high CPU",
            severity=3,
            level="yellow",
            hint=hint,
            reason=hint,
            suggested_action="monitor",
        )

    if snapshot.process_count >= policy.process_count_warn:
        hint = f"process tree has {snapshot.process_count} processes"
        return SessionHealth(
            label="many processes",
            severity=2,
            level="yellow",
            hint=hint,
            reason=hint,
            suggested_action="inspect children",
        )

    return SessionHealth(
        label="OK",
        severity=0,
        level="green",
        hint="within default resource budget",
        reason="within default resource budget",
    )


def classify_session(
    record: SessionRecord,
    snapshot: ResourceSnapshot,
    policy: HealthPolicy | None = None,
) -> SessionHealth:
    """Compatibility name used by GTK code."""

    return evaluate_health(record, snapshot, policy)


def health_badge(health: SessionHealth) -> str:
    """Plain-text badge safe for GTK labels, CLI, and logs."""

    return f"{health.label} ({health.level})"


def should_pause_for_budget(health: SessionHealth) -> bool:
    """Return whether the explicit Pause Hot action should include this task."""

    return health.severity >= 4 or health.label in {"critical memory", "critical CPU"}


def should_pause_for_attention(health: SessionHealth) -> bool:
    """Return whether the explicit Pause Hot action should include a task.

    This is never used automatically. It only gates a user-triggered bulk pause
    button so warnings such as "many processes" do not unexpectedly freeze a
    harmless tree.
    """

    return health.label in {"high CPU", "critical CPU", "high memory", "critical memory"}
