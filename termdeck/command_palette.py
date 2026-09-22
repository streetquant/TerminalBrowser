from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import SessionRecord


@dataclass(frozen=True)
class PaletteItem:
    """One browser-style command palette result.

    `kind` is intentionally small and stable so the GTK layer can dispatch
    actions without knowing how search ranking works.
    """

    id: str
    label: str
    kind: str
    description: str
    payload: str
    keywords: tuple[str, ...] = ()
    badge: str = ""
    destructive: bool = False

    @property
    def haystack(self) -> str:
        return " ".join(
            part
            for part in (
                self.id,
                self.label,
                self.kind,
                self.description,
                self.payload,
                self.badge,
                *self.keywords,
            )
            if part
        ).casefold()


def builtin_palette_items() -> tuple[PaletteItem, ...]:
    """Return global actions shown before remembered tabs."""
    return (
        PaletteItem(
            id="builtin:new-tab",
            label="New shell tab",
            kind="builtin",
            description="Open the default shell immediately in the home directory; use cd to move elsewhere.",
            payload="new_tab",
            keywords=("ctrl+t", "terminal", "home", "cwd", "shell", "cd"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:rename-window",
            label="Rename this window",
            kind="builtin",
            description="Name this t1000 window as a group of related directory tabs.",
            payload="rename_window",
            keywords=("window", "group", "name", "title"),
            badge="window",
        ),
        PaletteItem(
            id="builtin:new-window",
            label="New named window",
            kind="builtin",
            description="Create another named t1000 window and open its first shell immediately.",
            payload="new_window",
            keywords=("window", "group", "new"),
            badge="window",
        ),
        PaletteItem(
            id="builtin:reopen-closed",
            label="Reopen last closed tab",
            kind="builtin",
            description="Restore the most recently hidden browser tab without restarting the terminal process.",
            payload="reopen_closed",
            keywords=("closed", "recent", "undo", "ctrl+shift+t", "browser"),
            badge="browser",
        ),
        PaletteItem(
            id="builtin:master",
            label="Open Tab manager",
            kind="builtin",
            description="Inspect tabs with PID, CPU, RAM, runtime, focus key, and pause key.",
            payload="master",
            keywords=("tab manager", "resources", "cpu", "ram", "shift+esc", "ctrl+shift+m"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:search-sessions",
            label="Search all terminal tabs",
            kind="builtin",
            description="Search remembered tab metadata and optionally recent captured output, with redaction on by default.",
            payload="search_sessions",
            keywords=("find", "search", "tabs", "output", "capture", "browser", "ctrl+f"),
            badge="search",
        ),
        PaletteItem(
            id="builtin:shortcuts",
            label="Show keyboard shortcuts",
            kind="builtin",
            description="Open browser-style shortcuts, Terminator overrides, tab pause keys, and terminal passthrough keys.",
            payload="shortcuts",
            keywords=("keys", "keyboard", "emacs", "terminator", "pause", "alt+1", "ctrl+a"),
            badge="help",
        ),
        PaletteItem(
            id="builtin:windows",
            label="Open window overview",
            kind="builtin",
            description="Show every t1000 window with aggregate tabs, CPU, RAM, process count, runtime, and attention counts.",
            payload="window_overview",
            keywords=("windows", "overview", "workspace", "resource", "tabs", "cpu", "ram"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:activity",
            label="Open activity monitor",
            kind="builtin",
            description="Classify captured terminal output as waiting, error, warning, ready, silent, or active without exposing tmux.",
            payload="activity",
            keywords=("activity", "waiting", "input", "error", "capture", "preview", "status", "terminal"),
            badge="attention",
        ),
        PaletteItem(
            id="builtin:restore",
            label="Restore visible tabs",
            kind="builtin",
            description="Reattach the previous visible tabs for this window without restarting stopped processes.",
            payload="restore",
            keywords=("tabs", "workspace", "previous", "resume"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:toggle-tabs-pane",
            label="Show or hide Tabs pane",
            kind="builtin",
            description="Keep the main window terminal-first, or reveal advanced multi-tab controls when needed.",
            payload="toggle_side",
            keywords=("f9", "tabs pane", "sidebar", "advanced controls", "terminal first"),
            badge="view",
        ),
        PaletteItem(
            id="builtin:focus-side",
            label="Focus side-pane prompt box",
            kind="builtin",
            description="Move the keyboard cursor to the side pane's send-to-selected input.",
            payload="focus_side",
            keywords=("ctrl+shift+l", "prompt", "send"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:status-selected",
            label="Ask selected tabs for status",
            kind="builtin",
            description="Send a non-mutating status request to the tabs selected in the side pane.",
            payload="status_selected",
            keywords=("codex", "summary", "selected", "safe"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:terminal-options",
            label="Terminal options",
            kind="builtin",
            description="Configure font, cursor shape/colour, palette pack, and scrollback for VTE terminals.",
            payload="terminal_options",
            keywords=("font", "cursor", "scrollback", "palette", "appearance", "preferences"),
            badge="settings",
        ),
        PaletteItem(
            id="builtin:rename-current",
            label="Rename current tab",
            kind="builtin",
            description="Rename the active tab without restarting its process.",
            payload="rename_current",
            keywords=("rename", "title", "tab name", "f2"),
            badge="browser",
        ),
        PaletteItem(
            id="builtin:rename-selected",
            label="Rename selected tab",
            kind="builtin",
            description="Rename the first tab selected in the side pane, or the current tab.",
            payload="rename_selected",
            keywords=("rename", "selected", "tab name"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:pause-selected",
            label="Pause selected tabs",
            kind="builtin",
            description="Freeze execution of selected process groups until resumed.",
            payload="pause_selected",
            keywords=("sigstop", "freeze", "selected"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:resume-selected",
            label="Resume selected tabs",
            kind="builtin",
            description="Continue selected paused process groups.",
            payload="resume_selected",
            keywords=("sigcont", "unfreeze", "selected"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:pin-selected",
            label="Pin selected tabs",
            kind="builtin",
            description="Keep selected browser tabs visible, front-loaded, and protected from cleanup actions.",
            payload="pin_selected",
            keywords=("pin", "protect", "selected", "browser"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:unpin-selected",
            label="Unpin selected tabs",
            kind="builtin",
            description="Remove pinned protection from selected browser tabs without touching their processes.",
            payload="unpin_selected",
            keywords=("unpin", "protect", "selected", "browser"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:hide-selected",
            label="Hide selected tabs",
            kind="builtin",
            description="Remove selected tabs from visible restore without stopping their processes.",
            payload="hide_selected",
            keywords=("tabs", "browser", "selected", "restore", "lifecycle"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:pause-hot",
            label="Pause hot tabs",
            kind="builtin",
            description="Pause tabs classified as high or critical CPU/memory processes.",
            payload="pause_hot",
            keywords=("cpu", "ram", "memory", "resource", "budget", "runaway"),
            badge="resource",
        ),
        PaletteItem(
            id="builtin:kill-selected",
            label="Kill selected tabs",
            kind="builtin",
            description="Kill the selected tab processes and mark them stopped.",
            payload="kill_selected",
            keywords=("terminate", "close", "danger", "selected"),
            badge="destructive",
            destructive=True,
        ),
        PaletteItem(
            id="builtin:archive-selected",
            label="Archive selected tabs",
            kind="builtin",
            description="Remove selected tabs from normal views without stopping their processes.",
            payload="archive_selected",
            keywords=("hide", "cleanup", "browser", "lifecycle", "selected"),
            badge="selected",
        ),
    )


def session_palette_items(records: Iterable[SessionRecord]) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    for record in records:
        items.append(
            PaletteItem(
                id=f"session:{record.id}",
                label=f"Open {'📌 ' if record.pinned else ''}{record.name}",
                kind="session",
                description=f"{record.command} · {record.cwd}",
                payload=record.id,
                keywords=(
                    record.id,
                    record.name,
                    record.tag,
                    record.command,
                    record.cwd,
                    record.window_title,
                    record.status,
                    record.tmux_name,
                    "pinned" if record.pinned else "",
                ),
                badge=("pinned" if record.pinned else record.status),
            )
        )
    return items


def build_palette_items(records: Iterable[SessionRecord] = ()) -> list[PaletteItem]:
    """Build the command palette from app actions and remembered tabs."""

    return [*builtin_palette_items(), *session_palette_items(records)]


def _score(query: str, item: PaletteItem, order: int) -> int:
    if not query:
        return 10_000 - order
    query_cf = query.casefold().strip()
    terms = [term for term in query_cf.split() if term]
    if not terms or not all(term in item.haystack for term in terms):
        return -1
    score = 0
    label = item.label.casefold()
    identifier = item.id.casefold()
    payload = item.payload.casefold()
    if label.startswith(query_cf):
        score += 70
    if identifier.startswith(query_cf) or payload.startswith(query_cf):
        score += 40
    if query_cf in label:
        score += 25
    if query_cf in item.description.casefold():
        score += 10
    if item.kind in terms:
        score += 8
    score += max(0, 20 - order)
    return score


def search_palette(query: str, items: Sequence[PaletteItem], limit: int = 80) -> list[PaletteItem]:
    """Return palette items matching all query terms, ranked for browser-like use."""
    ranked: list[tuple[int, int, PaletteItem]] = []
    for order, item in enumerate(items):
        score = _score(query, item, order)
        if score >= 0:
            ranked.append((score, order, item))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2].label.casefold()))
    return [item for _score_value, _order, item in ranked[: max(1, int(limit))]]


def serialise_palette_items(items: Iterable[PaletteItem]) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "kind": item.kind,
            "description": item.description,
            "payload": item.payload,
            "keywords": list(item.keywords),
            "badge": item.badge,
            "destructive": item.destructive,
        }
        for item in items
    ]



def search_palette_items(query: str, items: Sequence[PaletteItem], limit: int = 80) -> list[PaletteItem]:
    """Compatibility alias for CLI and downstream callers."""

    return search_palette(query, items, limit=limit)
