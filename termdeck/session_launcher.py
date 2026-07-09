from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .models import SessionRecord
from .session_store import SessionStore
from .task_templates import render_command, template_by_key
from .tmux_backend import TmuxError


class SessionBackend(Protocol):
    available: bool
    def create_or_resume(self, record: SessionRecord) -> None: ...
    def pane_pid(self, record: SessionRecord) -> int | None: ...


@dataclass(frozen=True)
class LaunchFields:
    """Resolved New Tab fields before a session is saved or started."""

    name: str
    tag: str
    command: str
    cwd: str
    source: str


@dataclass(frozen=True)
class LaunchResult:
    """JSON-ready result of creating one browser-like terminal task."""

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


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _name_from_command(command: str) -> str:
    parts = command.split()
    if not parts:
        return "task"
    return parts[0].rsplit("/", 1)[-1] or "task"


def resolve_launch_fields(
    *,
    target: str | None = None,
    template_id: str | None = None,
    command: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    cwd: str | Path | None = None,
) -> LaunchFields:
    """Resolve a browser-style `new` request into final terminal fields."""

    working_dir = str(Path(cwd or os.getcwd()).expanduser())
    explicit_command = _clean(command)
    explicit_template = _clean(template_id)
    positional = _clean(target)

    if explicit_command:
        rendered = render_command(explicit_command, cwd=working_dir)
        return LaunchFields(
            name=_clean(name) or _name_from_command(rendered),
            tag=_clean(tag) or "default",
            command=rendered,
            cwd=working_dir,
            source="command",
        )

    template_key = explicit_template or positional or "shell"
    template = template_by_key(template_key)
    if template is not None:
        base_name, base_tag, base_command, base_cwd = template.materialise(working_dir)
        if not base_command.strip():
            raise ValueError(f"template {template.id!r} needs --command before it can be launched")
        return LaunchFields(
            name=_clean(name) or base_name,
            tag=_clean(tag) or base_tag,
            command=base_command,
            cwd=base_cwd,
            source=f"template:{template.id}",
        )

    if explicit_template:
        raise ValueError(f"unknown template: {explicit_template}")
    if positional:
        rendered = render_command(positional, cwd=working_dir)
        return LaunchFields(
            name=_clean(name) or _name_from_command(rendered),
            tag=_clean(tag) or "default",
            command=rendered,
            cwd=working_dir,
            source="command",
        )

    raise ValueError("new session requires a template or command")


def _next_tab_index(store: SessionStore, window_id: str) -> int:
    records = store.list(include_archived=True, window_id=window_id)
    return (max((record.tab_index for record in records), default=-1) + 1)


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
    """Create one browser-like terminal task while keeping tmux hidden."""

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
        "sessions": [result.to_dict() for result in results],
    }
