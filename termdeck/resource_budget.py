from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import ResourceSnapshot, SessionRecord
from .resources import format_bytes

CONFIG_DIR = Path.home() / ".config" / "termdeck"
DEFAULT_BUDGET_PATH = CONFIG_DIR / "budgets.json"

_MEMORY_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
}

_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}

_VALUE_UNIT_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?\s*$")


class BudgetConfigError(ValueError):
    """Raised when a resource budget file is invalid."""


@dataclass(frozen=True)
class ResourceBudget:
    """Resource limits for a group of browser-like terminal task tabs.

    `tag` is an exact tag match, or `*` for the fallback budget.  A missing
    limit means that metric is not considered.  `action` is advisory unless a
    caller explicitly runs a control command such as pause-over-budget.
    """

    name: str = "default"
    tag: str = "*"
    max_cpu_percent: float | None = 300.0
    max_memory_bytes: int | None = 4 * 1024 * 1024 * 1024
    max_processes: int | None = 96
    max_runtime_seconds: float | None = None
    action: str = "inspect"

    @property
    def is_global(self) -> bool:
        return self.tag in {"", "*", "all"}

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "tag": self.tag,
            "max_cpu_percent": self.max_cpu_percent,
            "max_memory_bytes": self.max_memory_bytes,
            "max_memory": format_bytes(self.max_memory_bytes) if self.max_memory_bytes is not None else None,
            "max_processes": self.max_processes,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_runtime": format_duration(self.max_runtime_seconds) if self.max_runtime_seconds is not None else None,
            "action": self.action,
        }


@dataclass(frozen=True)
class BudgetViolation:
    metric: str
    actual: float
    limit: float
    label: str
    severity: int

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "actual": round(self.actual, 2),
            "limit": round(self.limit, 2),
            "label": self.label,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class BudgetAssessment:
    record: SessionRecord
    budget: ResourceBudget
    snapshot: ResourceSnapshot
    violations: tuple[BudgetViolation, ...]

    @property
    def over_budget(self) -> bool:
        return bool(self.violations)

    @property
    def severity(self) -> int:
        return max((violation.severity for violation in self.violations), default=0)

    @property
    def suggested_action(self) -> str:
        if not self.over_budget:
            return "none"
        return self.budget.action or "inspect"

    @property
    def reason(self) -> str:
        if not self.violations:
            return "within budget"
        return "; ".join(violation.label for violation in self.violations)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.record.id,
            "name": self.record.name,
            "tag": self.record.tag,
            "window": self.record.window_title,
            "status": self.record.status,
            "pid": self.snapshot.pid,
            "exists": self.snapshot.exists,
            "budget": self.budget.as_dict(),
            "over_budget": self.over_budget,
            "severity": self.severity,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "violations": [violation.as_dict() for violation in self.violations],
            "cpu_percent": round(self.snapshot.cpu_percent, 1) if self.snapshot.exists else 0.0,
            "memory_bytes": self.snapshot.memory_bytes if self.snapshot.exists else 0,
            "memory": format_bytes(self.snapshot.memory_bytes) if self.snapshot.exists else "",
            "process_count": self.snapshot.process_count if self.snapshot.exists else 0,
            "runtime_seconds": round(self.snapshot.runtime_seconds, 1) if self.snapshot.exists else 0.0,
            "runtime": format_duration(self.snapshot.runtime_seconds) if self.snapshot.exists else "",
        }


def parse_memory(value: object | None) -> int | None:
    """Parse memory values such as `512MiB`, `2 GB`, or bytes as int."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BudgetConfigError(f"invalid memory value: {value!r}")
    if isinstance(value, (int, float)):
        if float(value) < 0:
            raise BudgetConfigError(f"memory must be non-negative: {value!r}")
        return int(value)
    match = _VALUE_UNIT_RE.match(str(value))
    if not match:
        raise BudgetConfigError(f"invalid memory value: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "b").casefold()
    multiplier = _MEMORY_UNITS.get(unit)
    if multiplier is None:
        raise BudgetConfigError(f"unsupported memory unit: {unit}")
    return int(amount * multiplier)


def parse_duration(value: object | None) -> float | None:
    """Parse durations such as `30m`, `2h`, or seconds as a number."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BudgetConfigError(f"invalid duration value: {value!r}")
    if isinstance(value, (int, float)):
        if float(value) < 0:
            raise BudgetConfigError(f"duration must be non-negative: {value!r}")
        return float(value)
    match = _VALUE_UNIT_RE.match(str(value))
    if not match:
        raise BudgetConfigError(f"invalid duration value: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "s").casefold()
    multiplier = _DURATION_UNITS.get(unit)
    if multiplier is None:
        raise BudgetConfigError(f"unsupported duration unit: {unit}")
    return amount * multiplier


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _float_limit(row: Mapping[str, object], *names: str) -> float | None:
    for name in names:
        if name in row and row[name] not in {None, ""}:
            try:
                value = float(row[name])
            except (TypeError, ValueError) as exc:
                raise BudgetConfigError(f"{name} must be a number") from exc
            if value < 0:
                raise BudgetConfigError(f"{name} must be non-negative")
            return value
    return None


def _int_limit(row: Mapping[str, object], *names: str) -> int | None:
    value = _float_limit(row, *names)
    return int(value) if value is not None else None


def budget_from_mapping(row: Mapping[str, object], *, fallback_name: str = "budget") -> ResourceBudget:
    if not isinstance(row, Mapping):
        raise BudgetConfigError("each budget must be an object")
    name = str(row.get("name") or fallback_name)
    tag = str(row.get("tag") or "*")
    action = str(row.get("action") or "inspect").strip().casefold() or "inspect"
    if action not in {"inspect", "monitor", "pause", "warn"}:
        raise BudgetConfigError(f"unsupported budget action: {action}")
    memory_value = row.get("max_memory", row.get("max_memory_bytes"))
    runtime_value = row.get("max_runtime", row.get("max_runtime_seconds"))
    return ResourceBudget(
        name=name,
        tag=tag,
        max_cpu_percent=_float_limit(row, "max_cpu_percent", "cpu_percent", "cpu"),
        max_memory_bytes=parse_memory(memory_value),
        max_processes=_int_limit(row, "max_processes", "processes"),
        max_runtime_seconds=parse_duration(runtime_value),
        action=action,
    )


def default_budgets() -> list[ResourceBudget]:
    return [
        ResourceBudget(name="default", tag="*", max_cpu_percent=300.0, max_memory_bytes=4 * 1024 * 1024 * 1024, max_processes=96, action="inspect"),
        ResourceBudget(name="ai-agent", tag="ai", max_cpu_percent=400.0, max_memory_bytes=8 * 1024 * 1024 * 1024, max_processes=128, action="inspect"),
    ]


def sample_budget_json() -> str:
    payload = {
        "budgets": [
            {
                "name": "default",
                "tag": "*",
                "max_cpu_percent": 300,
                "max_memory": "4GiB",
                "max_processes": 96,
                "action": "inspect",
            },
            {
                "name": "ai-agent",
                "tag": "ai",
                "max_cpu_percent": 400,
                "max_memory": "8GiB",
                "max_processes": 128,
                "action": "inspect",
            },
            {
                "name": "runaway-scratch",
                "tag": "scratch",
                "max_cpu_percent": 150,
                "max_memory": "1GiB",
                "max_runtime": "2h",
                "action": "pause",
            },
        ]
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def budgets_from_payload(payload: object) -> list[ResourceBudget]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = payload.get("budgets", [])
        if "default" in payload:
            rows = [payload["default"], *list(rows)]
    else:
        raise BudgetConfigError("budget file must be a JSON object or list")
    if not isinstance(rows, list):
        raise BudgetConfigError("budgets must be a list")
    budgets = [budget_from_mapping(row, fallback_name=f"budget-{index}") for index, row in enumerate(rows, start=1)]
    return budgets or default_budgets()


def load_budgets(path: str | Path | None = None) -> list[ResourceBudget]:
    budget_path = Path(path).expanduser() if path else DEFAULT_BUDGET_PATH
    if not budget_path.exists():
        return default_budgets()
    try:
        payload = json.loads(budget_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BudgetConfigError(f"invalid JSON in budget file: {budget_path}") from exc
    return budgets_from_payload(payload)


def select_budget(record: SessionRecord, budgets: Sequence[ResourceBudget]) -> ResourceBudget:
    exact = [budget for budget in budgets if not budget.is_global and budget.tag == record.tag]
    if exact:
        return exact[-1]
    global_rows = [budget for budget in budgets if budget.is_global]
    if global_rows:
        return global_rows[-1]
    return default_budgets()[0]


def _severity(actual: float, limit: float) -> int:
    if limit <= 0:
        return 4 if actual > 0 else 0
    ratio = actual / limit
    if ratio >= 2:
        return 4
    if ratio >= 1.25:
        return 3
    return 2


def evaluate_budget(record: SessionRecord, snapshot: ResourceSnapshot, budgets: Sequence[ResourceBudget] | None = None) -> BudgetAssessment:
    budget = select_budget(record, list(budgets or default_budgets()))
    violations: list[BudgetViolation] = []
    if not snapshot.exists:
        return BudgetAssessment(record=record, budget=budget, snapshot=snapshot, violations=())
    if budget.max_cpu_percent is not None and snapshot.cpu_percent > budget.max_cpu_percent:
        violations.append(BudgetViolation("cpu_percent", snapshot.cpu_percent, budget.max_cpu_percent, f"CPU {snapshot.cpu_percent:.1f}% > {budget.max_cpu_percent:.1f}%", _severity(snapshot.cpu_percent, budget.max_cpu_percent)))
    if budget.max_memory_bytes is not None and snapshot.memory_bytes > budget.max_memory_bytes:
        violations.append(BudgetViolation("memory_bytes", float(snapshot.memory_bytes), float(budget.max_memory_bytes), f"memory {format_bytes(snapshot.memory_bytes)} > {format_bytes(budget.max_memory_bytes)}", _severity(float(snapshot.memory_bytes), float(budget.max_memory_bytes))))
    if budget.max_processes is not None and snapshot.process_count > budget.max_processes:
        violations.append(BudgetViolation("process_count", float(snapshot.process_count), float(budget.max_processes), f"processes {snapshot.process_count} > {budget.max_processes}", _severity(float(snapshot.process_count), float(budget.max_processes))))
    if budget.max_runtime_seconds is not None and snapshot.runtime_seconds > budget.max_runtime_seconds:
        violations.append(BudgetViolation("runtime_seconds", snapshot.runtime_seconds, budget.max_runtime_seconds, f"runtime {format_duration(snapshot.runtime_seconds)} > {format_duration(budget.max_runtime_seconds)}", _severity(snapshot.runtime_seconds, budget.max_runtime_seconds)))
    return BudgetAssessment(record=record, budget=budget, snapshot=snapshot, violations=tuple(violations))


def assess_budgets(records: Iterable[SessionRecord], snapshots: Mapping[str, ResourceSnapshot], budgets: Sequence[ResourceBudget] | None = None) -> list[BudgetAssessment]:
    active_budgets = list(budgets or default_budgets())
    return [evaluate_budget(record, snapshots.get(record.id, ResourceSnapshot(pid=record.last_pid, exists=False)), active_budgets) for record in records]


def render_budget_table(assessments: Iterable[BudgetAssessment], *, violations_only: bool = False) -> str:
    rows = [item for item in assessments if item.over_budget or not violations_only]
    if not rows:
        return "No sessions over budget."
    headers = ["ID", "Window", "Name", "Tag", "Budget", "CPU", "RAM", "Procs", "Runtime", "Result", "Action"]
    values: list[list[str]] = []
    for item in rows:
        values.append([
            item.record.id,
            item.record.window_title,
            item.record.name,
            item.record.tag,
            item.budget.name,
            f"{item.snapshot.cpu_percent:.1f}%" if item.snapshot.exists else "-",
            format_bytes(item.snapshot.memory_bytes) if item.snapshot.exists else "-",
            str(item.snapshot.process_count) if item.snapshot.exists else "-",
            format_duration(item.snapshot.runtime_seconds) if item.snapshot.exists else "-",
            item.reason,
            item.suggested_action,
        ])
    widths = [len(header) for header in headers]
    for value_row in values:
        for index, value in enumerate(value_row):
            widths[index] = min(36, max(widths[index], len(value)))

    def clean(value: str, width: int) -> str:
        if len(value) > width:
            return value[: max(1, width - 1)] + "…"
        return value.ljust(width)

    lines = ["  ".join(clean(header, widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for value_row in values:
        lines.append("  ".join(clean(value, widths[index]) for index, value in enumerate(value_row)))
    return "\n".join(lines)
