from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import SessionRecord
from .task_templates import TaskTemplate, builtin_templates, render_command


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
    """Return global actions shown before sessions/templates."""
    return (
        PaletteItem(
            id="builtin:new-shell",
            label="New shell tab",
            kind="builtin",
            description="Open the default interactive shell in a new browser-like tab.",
            payload="new_shell",
            keywords=("ctrl+t", "terminal", "local"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:new-task",
            label="New task from editable template",
            kind="builtin",
            description="Open the task launcher where every template field remains editable.",
            payload="new_task",
            keywords=("launcher", "template", "command", "ctrl+alt+n"),
            badge="action",
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
            id="builtin:project-detect",
            label="Open detected project workspace",
            kind="builtin",
            description="Suggest and open browser-like tabs from the current directory: shell, dev server, tests, lint, Docker, logs, and optional agents.",
            payload="project_detect",
            keywords=("project", "detect", "workspace", "recipe", "package.json", "pytest", "docker", "onboarding"),
            badge="project",
        ),
        PaletteItem(
            id="builtin:master",
            label="Open master task window",
            kind="builtin",
            description="Show all sessions with PID, CPU, RAM, runtime, focus key, and pause key.",
            payload="master",
            keywords=("task manager", "resources", "cpu", "ram", "shift+esc", "ctrl+shift+m"),
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
            description="Show every TermDeck window with aggregate tabs, CPU, RAM, process count, runtime, and hot-session counts.",
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
            label="Restore visible sessions",
            kind="builtin",
            description="Reattach the previous visible sessions for this window without restarting stopped tasks.",
            payload="restore",
            keywords=("tabs", "workspace", "previous", "resume"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:toggle-side",
            label="Toggle side pane",
            kind="builtin",
            description="Show or hide the right-side session controller.",
            payload="toggle_side",
            keywords=("f9", "controller", "sidebar"),
            badge="action",
        ),
        PaletteItem(
            id="builtin:focus-mode",
            label="Toggle focus mode",
            kind="builtin",
            description="Hide side chrome for a terminal-first layout; toggle again to restore the session rail.",
            payload="focus_mode",
            keywords=("focus", "zen", "distraction free", "terminal first", "fullscreen work"),
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
            label="Ask selected sessions for status",
            kind="builtin",
            description="Send a non-mutating status request to the sessions selected in the side pane.",
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
            description="Rename the active browser tab/session without restarting its process.",
            payload="rename_current",
            keywords=("rename", "title", "tab name", "f2"),
            badge="browser",
        ),
        PaletteItem(
            id="builtin:rename-selected",
            label="Rename selected session",
            kind="builtin",
            description="Rename the first session selected in the side pane (or the current tab).",
            payload="rename_selected",
            keywords=("rename", "selected", "tab name"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:pause-selected",
            label="Pause selected sessions",
            kind="builtin",
            description="Stop execution of selected process groups until resumed.",
            payload="pause_selected",
            keywords=("sigstop", "freeze", "selected"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:resume-selected",
            label="Resume selected sessions",
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
            description="Remove selected sessions from visible restore without stopping their processes.",
            payload="hide_selected",
            keywords=("tabs", "browser", "selected", "restore", "lifecycle"),
            badge="selected",
        ),
        PaletteItem(
            id="builtin:pause-hot",
            label="Pause hot sessions",
            kind="builtin",
            description="Pause sessions classified as high or critical CPU/memory tasks.",
            payload="pause_hot",
            keywords=("cpu", "ram", "memory", "resource", "budget", "runaway"),
            badge="resource",
        ),
        PaletteItem(
            id="builtin:kill-selected",
            label="Stop selected sessions",
            kind="builtin",
            description="Kill selected hidden tmux sessions and mark them stopped.",
            payload="kill_selected",
            keywords=("terminate", "close", "danger", "selected"),
            badge="destructive",
            destructive=True,
        ),
        PaletteItem(
            id="builtin:archive-selected",
            label="Archive selected sessions",
            kind="builtin",
            description="Remove selected sessions from normal views without stopping their processes.",
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
                label=f"Open {'📌 ' if record.pinned else ''}[{record.tag}] {record.name}",
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


def template_palette_items(templates: Iterable[TaskTemplate] | None = None) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    for template in templates if templates is not None else builtin_templates():
        command = render_command(template.command)
        items.append(
            PaletteItem(
                id=f"template:{template.id}",
                label=f"Start {template.label}",
                kind="template",
                description=f"{command or 'editable command'} · {template.description}",
                payload=template.id,
                keywords=(
                    template.id,
                    template.label,
                    template.name,
                    template.tag,
                    template.command,
                    template.description,
                    *template.requires,
                ),
                badge=f"template/{template.tag}",
            )
        )
    return items


def build_palette_items(
    records: Iterable[SessionRecord] = (),
    templates: Iterable[TaskTemplate] | None = None,
) -> list[PaletteItem]:
    """Build the full command palette model in display priority order."""
    return [*builtin_palette_items(), *session_palette_items(records), *template_palette_items(templates)]


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
