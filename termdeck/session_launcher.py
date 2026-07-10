from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .command_render import default_shell, directory_tab_name, normalise_directory, render_command
from .models import SessionRecord
from .session_store import SessionStore
from .tmux_backend import TmuxError


class SessionBackend(Protocol):
    available: bool

    def create_or_resume(self, record: SessionRecord) -> None: ...

    def pane_pid(self, record: SessionRecord) -> int | None: ...


@dataclass(frozen=True)
class LaunchFields:
    """Resolved directory-tab fields before a session is saved or started."""

    name: str
    tag: str
    command: str
    cwd: str
    source: str


@dataclass(frozen=True)
class LaunchResult:
    """JSON-ready result of creating one terminal tab."""

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
    tmux_name: str
    status: str
    started: bool
    pid: int | None = None
    error: str = ""
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def as_dict(self) -> dict[str, object]:
        return self.to_dict()

    def to_tab_dict(self) -> dict[str, object]:
        """Return the public directory-tab shape without internal tag/tmux fields."""

        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "cwd": self.cwd,
            "window_id": self.window_id,
            "window_title": self.window_title,
            "tab_index": self.tab_index,
            "visible": self.visible,
            "pinned": self.pinned,
            "status": self.status,
            "started": self.started,
            "pid": self.pid,
            "error": self.error,
            "source": self.source,
        }


def _clean(value: str | None) -> str:
    return (value or "").strip()


def resolve_launch_fields(
    *,
    target: str | None = None,
    command: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    cwd: str | Path | None = None,
) -> LaunchFields:
    """Resolve a simple directory-backed tab request.

    ``target`` is treated as a directory when ``cwd`` is omitted. The default
    command is the user's shell, and the default tab name is the directory's
    final path component rather than an absolute path or task-template label.
    """

    directory = normalise_directory(cwd or target)
    if not Path(directory).is_dir():
        raise ValueError(f"tab directory does not exist: {directory}")

    explicit_command = _clean(command)
    final_command = render_command(explicit_command or default_shell(), cwd=directory)
    return LaunchFields(
        name=_clean(name) or directory_tab_name(directory),
        tag=_clean(tag) or "terminal",
        command=final_command,
        cwd=directory,
        source="command" if explicit_command else "shell",
    )


def _next_tab_index(store: SessionStore, window_id: str) -> int:
    records = store.list(include_archived=True, window_id=window_id)
    return max((record.tab_index for record in records), default=-1) + 1


def result_from_record(
    record: SessionRecord,
    *,
    source: str,
    started: bool,
    pid: int | None = None,
    error: str = "",
) -> LaunchResult:
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
        tmux_name=record.tmux_name,
        status=record.status,
        started=started,
        pid=pid if pid is not None else record.last_pid,
        error=error,
        source=source,
    )


def create_new_session(
    store: SessionStore,
    backend: SessionBackend,
    fields: LaunchFields,
    *,
    window_id: str = "main",
    window_title: str = "Main",
    tab_index: int | None = None,
    visible: bool = True,
    pinned: bool = False,
    start: bool = True,
) -> LaunchResult:
    """Create one directory-backed terminal tab while keeping tmux hidden."""

    final_tab_index = _next_tab_index(store, window_id) if tab_index is None else max(0, int(tab_index))
    record = store.create(
        fields.name,
        fields.tag,
        fields.command,
        fields.cwd,
        window_id=window_id or "main",
        window_title=window_title or "Main",
        tab_index=final_tab_index,
        visible=visible,
        pinned=pinned,
    )
    if not start:
        return result_from_record(record, source=fields.source, started=False)

    try:
        backend.create_or_resume(record)
        pid = backend.pane_pid(record)
        record = store.update(record.id, status="running", last_pid=pid, visible=visible)
        return result_from_record(record, source=fields.source, started=True, pid=pid)
    except TmuxError as exc:
        record = store.update(record.id, status="start-error", last_pid=None, visible=visible)
        return result_from_record(record, source=fields.source, started=False, error=str(exc))


def launch_payload(action: str, results: list[LaunchResult]) -> dict[str, object]:
    return {
        "action": action,
        "count": len(results),
        "ok": bool(results) and all(result.ok for result in results),
        "tabs": [result.to_tab_dict() for result in results],
    }
