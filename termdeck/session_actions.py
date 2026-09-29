from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .models import SessionRecord
from .session_store import SessionStore, short_id
from .tmux_backend import TmuxBackend, TmuxError
from .workspace_recipe import WorkspaceRecipe


@dataclass(frozen=True)
class LaunchResult:
    """JSON-ready result for a user-facing launch action.

    The GUI can ignore most fields, while CLI/external launchers can show the
    exact session capsule that was created without exposing tmux commands.
    """

    id: str
    name: str
    tag: str
    command: str
    cwd: str
    window_id: str
    window_title: str
    tab_index: int
    visible: bool
    pinned: bool
    status: str
    started: bool
    pid: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _result_from_record(record: SessionRecord, *, started: bool, pid: int | None = None, error: str = "") -> LaunchResult:
    return LaunchResult(
        id=record.id,
        name=record.name,
        tag=record.tag,
        command=record.command,
        cwd=record.cwd,
        window_id=record.window_id,
        window_title=record.window_title,
        tab_index=record.tab_index,
        visible=record.visible,
        pinned=record.pinned,
        status=record.status,
        started=started,
        pid=pid,
        error=error,
    )


def create_task_session(
    store: SessionStore,
    backend: TmuxBackend,
    *,
    name: str,
    tag: str,
    command: str,
    cwd: str | Path | None = None,
    window_id: str = "main",
    window_title: str = "Main",
    tab_index: int = 0,
    visible: bool = True,
    pinned: bool = False,
    automatic_name: bool = False,
    start: bool = True,
    fail_on_start_error: bool = False,
) -> LaunchResult:
    """Create one remembered terminal task and optionally start it.

    This is the headless equivalent of clicking New Tab in the GUI. tmux remains
    an implementation detail; callers receive a browser-style session capsule.
    """

    record = store.create(
        name=name,
        tag=tag,
        command=command,
        cwd=str(cwd) if cwd is not None else None,
        window_id=window_id,
        window_title=window_title,
        tab_index=tab_index,
        visible=visible,
        pinned=pinned,
        automatic_name=automatic_name,
    )
    if not start:
        record = store.update(record.id, status="remembered", last_pid=None)
        return _result_from_record(record, started=False)

    try:
        backend.create_or_resume(record)
        pid = backend.pane_pid(record)
        record = store.update(record.id, status="running", last_pid=pid)
        return _result_from_record(record, started=True, pid=pid)
    except TmuxError as exc:
        if fail_on_start_error:
            raise
        record = store.update(record.id, status="start-error", last_pid=None)
        return _result_from_record(record, started=False, error=str(exc))


def create_workspace_sessions(
    store: SessionStore,
    backend: TmuxBackend,
    recipe: WorkspaceRecipe,
    *,
    start: bool = True,
    fail_on_start_error: bool = False,
) -> list[LaunchResult]:
    """Materialise a workspace recipe into remembered browser-like sessions."""

    results: list[LaunchResult] = []
    for window_index, recipe_window in enumerate(recipe.windows, start=1):
        window_id = short_id("win")
        window_title = recipe_window.title or f"{recipe.name} {window_index}"
        for tab_index, tab in enumerate(recipe_window.tabs):
            results.append(
                create_task_session(
                    store,
                    backend,
                    name=tab.name,
                    tag=tab.tag,
                    command=tab.command,
                    cwd=tab.cwd,
                    window_id=window_id,
                    window_title=window_title,
                    tab_index=tab_index,
                    visible=tab.visible,
                    pinned=getattr(tab, "pinned", False),
                    automatic_name=tab.automatic_name,
                    start=start,
                    fail_on_start_error=fail_on_start_error,
                )
            )
    return results


def launch_results_payload(action: str, results: Iterable[LaunchResult]) -> dict[str, object]:
    rows = list(results)
    return {
        "action": action,
        "count": len(rows),
        "ok": all(row.ok for row in rows),
        "sessions": [row.to_dict() for row in rows],
    }
