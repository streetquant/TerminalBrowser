from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping, Sequence


@dataclass(frozen=True)
class TabShortcut:
    """Browser-like shortcuts shown in the master task window."""

    index: int
    focus: str
    pause_toggle: str


@dataclass(frozen=True)
class ShortcutBinding:
    """Resolved app shortcut with conflict and provenance metadata."""

    action: str
    label: str
    accelerator: str
    source: str = "default"
    terminal_conflict: str | None = None
    duplicate_with: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "label": self.label,
            "accelerator": self.accelerator,
            "source": self.source,
            "terminal_conflict": self.terminal_conflict,
            "duplicate_with": list(self.duplicate_with),
            "note": self.note,
        }


@dataclass(frozen=True)
class ShortcutEntry:
    """Flattened row used by older lightweight CLI/GUI shortcut views."""

    action: str
    accelerator: str
    scope: str = "app"
    source: str = "default"
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "accelerator": self.accelerator,
            "scope": self.scope,
            "source": self.source,
            "description": self.description,
        }


ACTION_LABELS: Mapping[str, str] = {
    "new_tab": "New shell tab",
    "new_command": "New custom command tab",
    "close_term": "Hide current tab, keep process alive",
    "reopen_closed": "Reopen last closed tab",
    "stop_session": "Stop current session",
    "next_tab": "Next tab",
    "prev_tab": "Previous tab",
    "focus_side": "Focus side-pane prompt",
    "launcher": "Open command palette",
    "task_launcher": "Open browser-style task launcher",
    "master_window": "Open master task window",
    "toggle_side": "Toggle side pane",
    "pause_current": "Pause/resume current tab process",
    "zoom_in": "Increase terminal font size",
    "zoom_out": "Decrease terminal font size",
    "zoom_reset": "Reset terminal font size",
    "copy": "Copy terminal selection",
    "paste": "Paste into terminal",
}

# Browser profile mirrors common browser tab muscle memory.  Ctrl+W is reported
# as a terminal conflict so users who rely on readline/emacs word deletion can
# switch to the emacs-safe profile.
DEFAULT_SHORTCUTS: Mapping[str, str] = {
    "new_tab": "<Primary>t",
    "new_command": "<Primary><Alt>t",
    "close_term": "<Primary>w",
    "reopen_closed": "<Primary><Shift>t",
    "stop_session": "<Primary><Shift>w",
    "next_tab": "<Primary>Tab",
    "prev_tab": "<Primary><Shift>Tab",
    "focus_side": "<Primary><Shift>l",
    "launcher": "<Primary><Shift>p",
    "task_launcher": "<Primary><Alt>n",
    "master_window": "<Primary><Shift>m",
    "toggle_side": "F9",
    "pause_current": "<Primary><Shift>space",
    "zoom_in": "<Primary>equal",
    "zoom_out": "<Primary>minus",
    "zoom_reset": "<Primary>0",
    # Terminal clipboard (Ctrl+Shift+C/V is classic; Ctrl+V also bound for paste).
    "copy": "<Primary><Shift>c",
    "paste": "<Primary><Shift>v",
}

EMACS_SAFE_SHORTCUTS: Mapping[str, str] = {
    **DEFAULT_SHORTCUTS,
    "close_term": "<Primary><Shift>w",
    "stop_session": "<Primary><Alt>w",
}

PROFILES: Mapping[str, Mapping[str, str]] = {
    "browser": DEFAULT_SHORTCUTS,
    "emacs-safe": EMACS_SAFE_SHORTCUTS,
}

TERMINATOR_IMPORT_ACTIONS = {"new_tab", "close_term", "next_tab", "prev_tab", "copy", "paste"}

PROTECTED_TERMINAL_KEYS: Mapping[str, str] = {
    "<primary>a": "shell/readline beginning of line",
    "<primary>b": "shell/readline backward character",
    "<primary>d": "shell/readline delete character / EOF",
    "<primary>e": "shell/readline end of line",
    "<primary>f": "shell/readline forward character",
    "<primary>k": "shell/readline kill to end of line",
    "<primary>l": "shell clear screen",
    "<primary>n": "shell/readline next history item",
    "<primary>p": "shell/readline previous history item",
    "<primary>r": "shell reverse search",
    "<primary>u": "shell/readline kill to start of line",
    "<primary>w": "shell/readline backward kill word",
    "<primary>y": "shell/readline yank",
}

PASSTHROUGH_LABELS: Mapping[str, str] = {
    "<primary>a": "Terminal line start",
    "<primary>b": "Terminal backward character",
    "<primary>d": "Terminal delete/EOF",
    "<primary>e": "Terminal line end",
    "<primary>f": "Terminal forward character",
    "<primary>k": "Terminal kill to line end",
    "<primary>l": "Terminal clear screen",
    "<primary>n": "Terminal next history",
    "<primary>p": "Terminal previous history",
    "<primary>r": "Terminal reverse search",
    "<primary>u": "Terminal kill to line start",
    "<primary>w": "Terminal kill previous word",
    "<primary>y": "Terminal yank",
}


def shortcut_profile_names() -> tuple[str, ...]:
    return tuple(PROFILES.keys())


def normalise_accelerator(accelerator: str) -> str:
    return "".join(str(accelerator or "").strip().lower().split())


def terminal_conflict_for(accelerator: str) -> str | None:
    return PROTECTED_TERMINAL_KEYS.get(normalise_accelerator(accelerator))


def _rejects_terminator_binding(profile: str, accelerator: str) -> str:
    conflict = terminal_conflict_for(accelerator)
    if profile == "emacs-safe" and conflict:
        return f"ignored Terminator binding because it conflicts with {conflict}"
    return ""


def build_shortcut_map(
    terminator_keybindings: Mapping[str, str] | None = None,
    *,
    profile: str = "browser",
    import_terminator: bool = True,
) -> dict[str, str]:
    if profile not in PROFILES:
        raise ValueError(f"unknown shortcut profile: {profile}")
    resolved = dict(PROFILES[profile])
    if import_terminator:
        for action, accelerator in (terminator_keybindings or {}).items():
            if action not in TERMINATOR_IMPORT_ACTIONS or not accelerator:
                continue
            if _rejects_terminator_binding(profile, accelerator):
                continue
            resolved[action] = accelerator
    return resolved


def build_shortcut_bindings(
    terminator_keybindings: Mapping[str, str] | None = None,
    *,
    profile: str = "browser",
    import_terminator: bool = True,
) -> list[ShortcutBinding]:
    if profile not in PROFILES:
        raise ValueError(f"unknown shortcut profile: {profile}")
    defaults = dict(PROFILES[profile])
    resolved = build_shortcut_map(terminator_keybindings, profile=profile, import_terminator=import_terminator)
    notes: dict[str, str] = {}
    sources: dict[str, str] = {action: "default" for action in resolved}

    if import_terminator:
        for action, accelerator in (terminator_keybindings or {}).items():
            if action not in TERMINATOR_IMPORT_ACTIONS or not accelerator:
                continue
            note = _rejects_terminator_binding(profile, accelerator)
            if note:
                notes[action] = note
                continue
            if resolved.get(action) == accelerator and defaults.get(action) != accelerator:
                sources[action] = "terminator"

    by_accelerator: dict[str, list[str]] = {}
    for action, accelerator in resolved.items():
        if accelerator:
            by_accelerator.setdefault(normalise_accelerator(accelerator), []).append(action)

    ordered_actions = sorted(resolved, key=lambda name: (name not in DEFAULT_SHORTCUTS, name))
    bindings: list[ShortcutBinding] = []
    for action in ordered_actions:
        accelerator = resolved[action]
        duplicates = tuple(other for other in by_accelerator.get(normalise_accelerator(accelerator), []) if other != action)
        bindings.append(ShortcutBinding(
            action=action,
            label=ACTION_LABELS.get(action, action.replace("_", " ").title()),
            accelerator=accelerator,
            source=sources.get(action, "default"),
            terminal_conflict=terminal_conflict_for(accelerator),
            duplicate_with=duplicates,
            note=notes.get(action, ""),
        ))
    return bindings


def build_shortcut_report(
    terminator_keybindings: Mapping[str, str] | None = None,
    *,
    profile: str = "browser",
    import_terminator: bool = True,
) -> dict[str, object]:
    bindings = build_shortcut_bindings(terminator_keybindings, profile=profile, import_terminator=import_terminator)
    duplicate_rows = [binding.as_dict() for binding in bindings if binding.duplicate_with]
    terminal_conflicts = [binding.as_dict() for binding in bindings if binding.terminal_conflict]
    return {
        "schema": "termdeck.shortcuts",
        "profile": profile,
        "import_terminator": import_terminator,
        "profiles": list(shortcut_profile_names()),
        "protected_terminal_keys": dict(PROTECTED_TERMINAL_KEYS),
        "duplicate_count": len(duplicate_rows),
        "terminal_conflict_count": len(terminal_conflicts),
        "duplicates": duplicate_rows,
        "terminal_conflicts": terminal_conflicts,
        "shortcuts": [binding.as_dict() for binding in bindings],
    }


def _display_accelerator(accelerator: str) -> str:
    display = (
        str(accelerator)
        .replace("<Primary>", "Ctrl+")
        .replace("<primary>", "Ctrl+")
        .replace("<Shift>", "Shift+")
        .replace("<shift>", "Shift+")
        .replace("<Alt>", "Alt+")
        .replace("<alt>", "Alt+")
        .replace("<Super>", "Super+")
        .replace("<super>", "Super+")
    )
    return re.sub(r"(?<=\+)([a-z])(?=$|\+)", lambda match: match.group(1).upper(), display)


def _visible_index(index: int) -> int | None:
    if 1 <= index <= 9:
        return index
    return None


def focus_shortcut_for_index(index: int) -> str:
    number = _visible_index(index)
    return f"Alt+{number}" if number is not None else ""


def pause_shortcut_for_index(index: int) -> str:
    number = _visible_index(index)
    return f"Ctrl+Alt+{number}" if number is not None else ""


def focus_shortcut(index: int) -> str:
    return focus_shortcut_for_index(index)


def pause_shortcut(index: int) -> str:
    return pause_shortcut_for_index(index)


def tab_shortcut(index: int) -> TabShortcut:
    return TabShortcut(index=index, focus=focus_shortcut_for_index(index), pause_toggle=pause_shortcut_for_index(index))


def index_from_key_name(key_name: str) -> int | None:
    if len(key_name) == 1 and key_name.isdigit():
        value = int(key_name)
        return value if 1 <= value <= 9 else None
    if key_name.startswith("KP_") and key_name[3:].isdigit():
        value = int(key_name[3:])
        return value if 1 <= value <= 9 else None
    return None


def shortcut_entries(
    terminator_keybindings: Mapping[str, str] | None = None,
    *,
    profile: str = "browser",
    import_terminator: bool = True,
) -> list[ShortcutEntry]:
    rows: list[ShortcutEntry] = []
    report = build_shortcut_report(terminator_keybindings, profile=profile, import_terminator=import_terminator)
    for item in report.get("shortcuts", []):
        if not isinstance(item, Mapping):
            continue
        action = str(item.get("action") or "")
        scope = "app"
        if action in {"pause_current", "stop_session"}:
            scope = "process"
        elif action in {"copy", "paste"}:
            scope = "terminal"
        notes = []
        if item.get("terminal_conflict"):
            notes.append(f"terminal conflict: {item['terminal_conflict']}")
        if item.get("duplicate_with"):
            notes.append("duplicate accelerator")
        if item.get("note"):
            notes.append(str(item["note"]))
        rows.append(ShortcutEntry(
            action=str(item.get("label") or action),
            accelerator=str(item.get("accelerator") or ""),
            scope=scope,
            source=str(item.get("source") or "default"),
            description="; ".join(notes),
        ))
    for index in range(1, 10):
        rows.append(ShortcutEntry(f"Focus tab {index}", focus_shortcut_for_index(index), "tab", "generated", "Browser-style direct tab focus shortcut"))
        rows.append(ShortcutEntry(f"Pause/resume tab {index}", pause_shortcut_for_index(index), "process", "generated", "Pause/resume numbered tab process"))
    for accelerator, description in sorted(PROTECTED_TERMINAL_KEYS.items()):
        rows.append(ShortcutEntry(
            action=PASSTHROUGH_LABELS.get(accelerator, description.title()),
            accelerator=_display_accelerator(accelerator.replace("<primary>", "<Primary>")),
            scope="terminal",
            source="passthrough",
            description=description,
        ))
    return rows


def shortcut_entries_to_dicts(entries: Sequence[ShortcutEntry]) -> list[dict[str, str]]:
    return [entry.as_dict() for entry in entries]


def render_shortcut_table(data: Mapping[str, object] | Sequence[ShortcutEntry]) -> str:
    if isinstance(data, Mapping):
        items = list(data.get("shortcuts", []))
        if not items:
            return "No shortcuts configured."
        rows = [["Action", "Shortcut", "Source", "Notes"]]
        for item in items:
            if not isinstance(item, Mapping):
                continue
            notes: list[str] = []
            if item.get("terminal_conflict"):
                notes.append(f"terminal conflict: {item['terminal_conflict']}")
            if item.get("duplicate_with"):
                notes.append("duplicate with " + ", ".join(str(x) for x in item["duplicate_with"]))
            if item.get("note"):
                notes.append(str(item["note"]))
            rows.append([
                str(item.get("label") or item.get("action") or ""),
                _display_accelerator(str(item.get("accelerator") or "")),
                str(item.get("source") or ""),
                "; ".join(notes),
            ])
    else:
        entries = list(data)
        if not entries:
            return "No shortcuts configured."
        rows = [["Action", "Shortcut", "Scope", "Source", "Description"]]
        for entry in entries:
            rows.append([entry.action, _display_accelerator(entry.accelerator), entry.scope, entry.source, entry.description])

    widths = [max(len(row[idx]) for row in rows) for idx in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(rows[0]))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows[1:]:
        lines.append("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))
    return "\n".join(lines)
