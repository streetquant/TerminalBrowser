from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import SessionRecord
from .session_store import SessionStore, short_id, tmux_safe_name, utc_now
from .tmux_backend import TmuxBackend, TmuxError
from .workspace_recipe import WorkspaceRecipe, WorkspaceRecipeError, load_recipe


class WorkspaceLaunchError(RuntimeError):
    """Raised when a workspace recipe cannot be opened."""


class SessionBackend(Protocol):
    @property
    def available(self) -> bool: ...

    def create_or_resume(self, record: SessionRecord) -> None: ...

    def pane_pid(self, record: SessionRecord) -> int | None: ...


@dataclass(frozen=True)
class WorkspaceLaunchRow:
    workspace: str
    window_id: str
    window_title: str
    tab_index: int
    session_id: str
    name: str
    tag: str
    command: str
    cwd: str
    visible: bool
    pinned: bool
    status: str
    pid: int | None
    created: bool
    reused: bool
    started: bool
    tmux_name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace,
            "window_id": self.window_id,
            "window_title": self.window_title,
            "tab_index": self.tab_index,
            "session_id": self.session_id,
            "name": self.name,
            "tag": self.tag,
            "command": self.command,
            "cwd": self.cwd,
            "visible": self.visible,
            "pinned": self.pinned,
            "status": self.status,
            "pid": self.pid,
            "created": self.created,
            "reused": self.reused,
            "started": self.started,
            "tmux_name": self.tmux_name,
        }


@dataclass(frozen=True)
class WorkspaceLaunchResult:
    workspace: str
    dry_run: bool
    start: bool
    rows: tuple[WorkspaceLaunchRow, ...]

    @property
    def count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace,
            "dry_run": self.dry_run,
            "start": self.start,
            "count": self.count,
            "sessions": [row.to_dict() for row in self.rows],
        }


def _window_id(workspace: str, window_index: int, *, stable: bool) -> str:
    prefix = tmux_safe_name(workspace).lower().strip("_") or "workspace"
    return f"{prefix}_{window_index:02d}" if stable else short_id("win")


def _dry_tmux_name(workspace: str, window_index: int, tab_index: int, name: str, tag: str) -> str:
    return tmux_safe_name(f"termdeck_{tag}_{workspace}_{window_index:02d}_{tab_index:02d}_{name}")


def dry_run_workspace(recipe: WorkspaceRecipe, *, start: bool = True) -> WorkspaceLaunchResult:
    rows: list[WorkspaceLaunchRow] = []
    for window_index, window in enumerate(recipe.windows, start=1):
        window_id = _window_id(recipe.name, window_index, stable=True)
        for zero_index, tab in enumerate(window.tabs):
            rows.append(
                WorkspaceLaunchRow(
                    workspace=recipe.name,
                    window_id=window_id,
                    window_title=window.title,
                    tab_index=zero_index,
                    session_id=f"planned-{window_index:02d}-{zero_index + 1:02d}",
                    name=tab.name,
                    tag=tab.tag,
                    command=tab.command,
                    cwd=tab.cwd,
                    visible=tab.visible,
                    pinned=tab.pinned,
                    status="planned-start" if start else "planned-register",
                    pid=None,
                    created=False,
                    reused=False,
                    started=False,
                    tmux_name=_dry_tmux_name(recipe.name, window_index, zero_index + 1, tab.name, tab.tag),
                )
            )
    return WorkspaceLaunchResult(workspace=recipe.name, dry_run=True, start=start, rows=tuple(rows))


def _find_reusable_record(store: SessionStore, *, name: str, tag: str, command: str, cwd: str, window_title: str) -> SessionRecord | None:
    return store.find_equivalent(name=name, tag=tag, command=command, cwd=cwd, window_title=window_title)


def open_workspace(
    recipe: WorkspaceRecipe,
    store: SessionStore,
    *,
    backend: SessionBackend | None = None,
    start: bool = True,
    dry_run: bool = False,
    reuse: bool = True,
) -> WorkspaceLaunchResult:
    if dry_run:
        return dry_run_workspace(recipe, start=start)

    backend = backend or TmuxBackend()
    if start and not backend.available:
        raise WorkspaceLaunchError("tmux is required to start a workspace; use --no-start to only register it")

    rows: list[WorkspaceLaunchRow] = []
    for window_index, window in enumerate(recipe.windows, start=1):
        window_id = _window_id(recipe.name, window_index, stable=False)
        for tab_index, tab in enumerate(window.tabs):
            existing = (
                _find_reusable_record(store, name=tab.name, tag=tab.tag, command=tab.command, cwd=tab.cwd, window_title=window.title)
                if reuse
                else None
            )
            if existing:
                record = store.update(
                    existing.id,
                    window_id=window_id,
                    window_title=window.title,
                    tab_index=tab_index,
                    visible=tab.visible,
                    pinned=tab.pinned,
                    status="new" if existing.status == "stopped" else existing.status,
                    last_opened_at=utc_now(),
                )
                created = False
                reused = True
            else:
                record = store.create(
                    name=tab.name,
                    tag=tab.tag,
                    command=tab.command,
                    cwd=tab.cwd,
                    window_id=window_id,
                    window_title=window.title,
                    tab_index=tab_index,
                    visible=tab.visible,
                    pinned=tab.pinned,
                )
                created = True
                reused = False

            pid = record.last_pid
            status = record.status
            started = False
            if start:
                try:
                    backend.create_or_resume(record)
                    pid = backend.pane_pid(record)
                    status = "running"
                    record = store.update(record.id, status=status, last_pid=pid)
                    started = True
                except TmuxError as exc:
                    raise WorkspaceLaunchError(str(exc)) from exc
            elif status not in {"running", "paused"}:
                record = store.update(record.id, status="new", last_pid=None)
                status = record.status
                pid = record.last_pid

            rows.append(
                WorkspaceLaunchRow(
                    workspace=recipe.name,
                    window_id=record.window_id,
                    window_title=record.window_title,
                    tab_index=record.tab_index,
                    session_id=record.id,
                    name=record.name,
                    tag=record.tag,
                    command=record.command,
                    cwd=record.cwd,
                    visible=record.visible,
                    pinned=record.pinned,
                    status=status,
                    pid=pid,
                    created=created,
                    reused=reused,
                    started=started,
                    tmux_name=record.tmux_name,
                )
            )
    return WorkspaceLaunchResult(workspace=recipe.name, dry_run=False, start=start, rows=tuple(rows))


def open_workspace_file(
    path: str | Path,
    store: SessionStore,
    *,
    backend: SessionBackend | None = None,
    start: bool = True,
    dry_run: bool = False,
    reuse: bool = True,
) -> WorkspaceLaunchResult:
    try:
        recipe = load_recipe(path)
    except WorkspaceRecipeError:
        raise
    return open_workspace(recipe, store, backend=backend, start=start, dry_run=dry_run, reuse=reuse)
