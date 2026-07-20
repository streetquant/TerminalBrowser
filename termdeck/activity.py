from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from .models import SessionRecord
from .redaction import redact_text

ActivitySortKey = Literal["severity", "state", "name", "tag", "window", "tab"]


@dataclass(frozen=True)
class ActivityItem:
    """One browser-task activity row derived from captured terminal output.

    This is deliberately separate from CPU/RAM health. A session can be cheap
    but still need attention because it is waiting for confirmation, or it can
    consume resources while producing normal output. The classifier uses only
    local captured terminal text and redacts by default before presenting it.
    """

    record: SessionRecord
    state: str
    level: str
    severity: int
    reason: str
    suggested_action: str
    preview: str
    redacted: bool = False
    redaction_labels: tuple[str, ...] = ()

    @property
    def search_text(self) -> str:
        return " ".join(
            [
                self.record.id,
                self.record.name,
                self.record.tag,
                self.record.window_title,
                self.record.status,
                self.record.command,
                self.record.cwd,
                self.state,
                self.level,
                self.reason,
                self.suggested_action,
                self.preview,
            ]
        ).casefold()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.record.id,
            "window": self.record.window_title,
            "name": self.record.name,
            "tag": self.record.tag,
            "status": self.record.status,
            "state": self.state,
            "level": self.level,
            "severity": self.severity,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "preview": self.preview,
            "redacted": self.redacted,
            "redaction_labels": list(self.redaction_labels),
            "command": self.record.command,
            "cwd": self.record.cwd,
            "visible": self.record.visible,
            "tab_index": self.record.tab_index,
        }


@dataclass(frozen=True)
class _Rule:
    state: str
    level: str
    severity: int
    pattern: re.Pattern[str]
    reason: str
    suggested_action: str


_WAITING_RULES = (
    _Rule("needs input", "orange", 4, re.compile(r"\b(password|passphrase)\s*:\s*$", re.IGNORECASE | re.MULTILINE), "terminal is asking for a secret or passphrase", "focus tab and respond manually"),
    _Rule("needs input", "orange", 4, re.compile(r"\b(continue|proceed|approve|confirm|allow|accept)\b[^\n]{0,80}(\?|\[y/n\]|\[y/N\]|\(y/n\)|\(yes/no\))", re.IGNORECASE), "terminal is waiting for confirmation", "focus tab or send an intentional answer"),
    _Rule("needs input", "orange", 4, re.compile(r"\bpress\s+(enter|return|any key)\b", re.IGNORECASE), "terminal is waiting for a key press", "focus tab and continue"),
    _Rule("needs input", "orange", 4, re.compile(r"\b(select|choose)\s+(an?\s+)?(option|number|choice)\b", re.IGNORECASE), "terminal is waiting for a menu selection", "focus tab and choose an option"),
    _Rule("needs input", "orange", 4, re.compile(r"\b(waiting|awaiting)\s+for\s+(user|input|approval|confirmation)\b", re.IGNORECASE), "terminal reports that it is waiting for input", "focus tab or send a prompt"),
)

_ERROR_RULES = (
    _Rule("error", "red", 4, re.compile(r"\bTraceback \(most recent call last\):", re.IGNORECASE), "Python traceback detected", "inspect failure"),
    _Rule("error", "red", 4, re.compile(r"\b(fatal|panic|segmentation fault|core dumped)\b", re.IGNORECASE), "fatal process error detected", "inspect or restart"),
    _Rule("error", "red", 4, re.compile(r"\b(npm ERR!|ERR_PNPM|yarn error|cargo: error)\b", re.IGNORECASE), "tool error detected", "inspect failure"),
    _Rule("error", "red", 4, re.compile(r"\b(failed|failure|error:)\b", re.IGNORECASE), "error-like output detected", "inspect failure"),
)

_WARNING_RULES = (
    _Rule("warning", "yellow", 2, re.compile(r"\b(warn|warning|deprecated|retrying|rate limit)\b", re.IGNORECASE), "warning-like output detected", "monitor or inspect"),
)

_SUCCESS_RULES = (
    _Rule("ready", "green", 0, re.compile(r"\b(listening on|server ready|ready in|compiled successfully|watching for file changes)\b", re.IGNORECASE), "long-running task appears ready", "leave running"),
    _Rule("success", "green", 0, re.compile(r"\b(all tests passed|tests? passed|passed\b|successfully|done in|OK\b)\b", re.IGNORECASE), "success-like output detected", "leave running or archive when finished"),
)


def _normalise_lines(text: str, *, max_lines: int = 18) -> list[str]:
    lines = [line.rstrip() for line in text.replace("\r", "\n").splitlines()]
    visible = [line for line in lines if line.strip()]
    return visible[-max_lines:]


def _preview(text: str, *, max_chars: int = 220) -> str:
    lines = _normalise_lines(text, max_lines=6)
    if not lines:
        return ""
    one_line = "  ⏎  ".join(line.strip() for line in lines if line.strip())
    one_line = re.sub(r"\s+", " ", one_line).strip()
    if len(one_line) > max_chars:
        return one_line[: max(1, max_chars - 1)] + "…"
    return one_line


def _first_matching_rule(text: str, rules: Iterable[_Rule]) -> _Rule | None:
    for rule in rules:
        if rule.pattern.search(text):
            return rule
    return None


def classify_activity(record: SessionRecord, capture: str, *, capture_available: bool = True, raw: bool = False) -> ActivityItem:
    """Classify one session's captured terminal output.

    The classifier favours user-attention states over generic errors because a
    session asking for confirmation is the most actionable browser-tab signal.
    Secrets are redacted unless ``raw`` is requested by the caller.
    """

    if not capture_available:
        return ActivityItem(
            record=record,
            state="unavailable",
            level="grey",
            severity=2,
            reason="capture is not available for this session",
            suggested_action="open or restore session",
            preview="",
        )

    redaction = None if raw else redact_text(capture)
    text = capture if raw else redaction.text
    tail = "\n".join(_normalise_lines(text, max_lines=24))
    preview = _preview(text)
    redacted = bool(redaction and redaction.replacements)
    labels = tuple(redaction.labels) if redaction else ()

    if not tail.strip():
        if record.status == "new":
            return ActivityItem(record, "not started", "grey", 1, "no terminal output has been produced yet", "open session", preview, redacted, labels)
        if record.status == "recovery-pending":
            return ActivityItem(record, "recovery pending", "red", 3, "exact process recovery has not completed", "inspect checkpoint status", preview, redacted, labels)
        if record.status in {"stopped", "archived"}:
            return ActivityItem(record, record.status, "grey", 1, f"session is {record.status}", "restart or archive", preview, redacted, labels)
        return ActivityItem(record, "silent", "grey", 1, "no recent visible output", "focus only if unexpected", preview, redacted, labels)

    for rules in (_WAITING_RULES, _ERROR_RULES, _WARNING_RULES, _SUCCESS_RULES):
        rule = _first_matching_rule(tail, rules)
        if rule is not None:
            return ActivityItem(record, rule.state, rule.level, rule.severity, rule.reason, rule.suggested_action, preview, redacted, labels)

    return ActivityItem(record, "active", "green", 0, "recent output without attention pattern", "leave running", preview, redacted, labels)


def scan_activities(records: Iterable[SessionRecord], *, backend, lines: int = 80, raw: bool = False) -> list[ActivityItem]:
    """Capture and classify remembered sessions through the supplied backend.

    ``backend`` only needs a ``capture(record, lines=...)`` method, which keeps
    this code testable without GTK/VTE/tmux.
    """

    items: list[ActivityItem] = []
    clean_lines = max(1, int(lines))
    for record in records:
        try:
            capture = backend.capture(record, lines=clean_lines)
        except Exception:
            items.append(classify_activity(record, "", capture_available=False, raw=raw))
            continue
        items.append(classify_activity(record, capture or "", capture_available=True, raw=raw))
    return items


def filter_activities(items: Iterable[ActivityItem], query: str) -> list[ActivityItem]:
    tokens = [token for token in query.casefold().split() if token]
    item_list = list(items)
    if not tokens:
        return item_list
    return [item for item in item_list if all(token in item.search_text for token in tokens)]


def sort_activities(items: Iterable[ActivityItem], key: str = "severity") -> list[ActivityItem]:
    item_list = list(items)
    if key == "state":
        return sorted(item_list, key=lambda item: (item.state.casefold(), -item.severity, item.record.name.casefold()))
    if key == "name":
        return sorted(item_list, key=lambda item: (item.record.name.casefold(), item.record.tag.casefold()))
    if key == "tag":
        return sorted(item_list, key=lambda item: (item.record.tag.casefold(), item.record.name.casefold()))
    if key == "window":
        return sorted(item_list, key=lambda item: (item.record.window_title.casefold(), item.record.tab_index, item.record.name.casefold()))
    if key == "tab":
        return sorted(item_list, key=lambda item: (item.record.tab_index, item.record.window_title.casefold(), item.record.name.casefold()))
    return sorted(item_list, key=lambda item: (-item.severity, item.record.window_title.casefold(), item.record.tab_index, item.record.name.casefold()))


def activity_to_dicts(items: Iterable[ActivityItem]) -> list[dict[str, object]]:
    return [item.as_dict() for item in items]


def render_activity_table(items: Iterable[ActivityItem]) -> str:
    rows = list(items)
    if not rows:
        return "No remembered sessions."
    headers = ["ID", "Window", "Name", "Tag", "Activity", "Reason", "Action", "Preview"]
    values = [
        [
            item.record.id,
            item.record.window_title,
            item.record.name,
            item.record.tag,
            f"{item.state} ({item.level})",
            item.reason,
            item.suggested_action,
            item.preview or "-",
        ]
        for item in rows
    ]
    widths = [len(header) for header in headers]
    caps = [14, 18, 22, 16, 20, 32, 26, 48]
    for value_row in values:
        for index, value in enumerate(value_row):
            widths[index] = min(caps[index], max(widths[index], len(value)))

    def clean(value: str, width: int) -> str:
        if len(value) > width:
            return value[: max(1, width - 1)] + "…"
        return value.ljust(width)

    lines = ["  ".join(clean(header, widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for value_row in values:
        lines.append("  ".join(clean(value, widths[index]) for index, value in enumerate(value_row)))
    return "\n".join(lines)
