from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol

from .models import SessionRecord
from .session_store import SessionStore, short_id, utc_now
from .tmux_backend import TmuxError

_FIELD_SEP = "\x1f"

TMUX_PANE_FORMAT = _FIELD_SEP.join(
    [
        "#{session_name}",
        "#{session_id}",
        "#{session_attached}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_active}",
        "#{pane_dead}",
    ]
)


class TmuxPaneLister(Protocol):
    available: bool
    def list_panes_text(self, format_string: str) -> str: ...


@dataclass(frozen=True)
class TmuxPaneInfo:
    session_name: str
    session_id: str
    session_attached: bool
    window_index: int
    window_name: str
    pane_index: int
    pane_id: str
    pane_pid: int | None
    current_command: str
    current_path: str
    pane_active: bool
    pane_dead: bool

    @property
    def target(self) -> str:
        return self.pane_id or f"{self.session_name}:{self.window_index}.{self.pane_index}"

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"target": self.target}


@dataclass(frozen=True)
class TmuxSessionInfo:
    session_name: str
    session_id: str
    attached: bool
    pane_count: int
    active_pane: TmuxPaneInfo

    @property
    def command(self) -> str:
        return self.active_pane.current_command or os.environ.get("SHELL", "/bin/sh")

    @property
    def cwd(self) -> str:
        return self.active_pane.current_path or os.getcwd()

    @property
    def pid(self) -> int | None:
        return self.active_pane.pane_pid

    @property
    def pane_target(self) -> str:
        return self.active_pane.target

    def to_dict(self) -> dict[str, object]:
        return {
            "session_name": self.session_name,
            "session_id": self.session_id,
            "attached": self.attached,
            "pane_count": self.pane_count,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.pid,
            "pane_target": self.pane_target,
            "active_pane": self.active_pane.to_dict(),
        }


@dataclass(frozen=True)
class AdoptTmuxResult:
    tmux_name: str
    status: str
    ok: bool
    id: str | None = None
    name: str = ""
    tag: str = ""
    command: str = ""
    cwd: str = ""
    pid: int | None = None
    pane_target: str = ""
    pane_count: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _to_bool(value: str) -> bool:
    return value.strip() not in {"", "0", "false", "False", "no", "No"}


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def parse_tmux_panes(text: str) -> list[TmuxPaneInfo]:
    """Parse ``tmux list-panes`` output generated with ``TMUX_PANE_FORMAT``."""

    panes: list[TmuxPaneInfo] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split(_FIELD_SEP)
        if len(parts) != 12:
            continue
        panes.append(
            TmuxPaneInfo(
                session_name=parts[0],
                session_id=parts[1],
                session_attached=_to_bool(parts[2]),
                window_index=_to_int(parts[3]),
                window_name=parts[4],
                pane_index=_to_int(parts[5]),
                pane_id=parts[6],
                pane_pid=_to_int(parts[7]) or None,
                current_command=parts[8],
                current_path=parts[9],
                pane_active=_to_bool(parts[10]),
                pane_dead=_to_bool(parts[11]),
            )
        )
    return panes


def group_tmux_sessions(panes: Iterable[TmuxPaneInfo]) -> list[TmuxSessionInfo]:
    grouped: dict[str, list[TmuxPaneInfo]] = {}
    for pane in panes:
        grouped.setdefault(pane.session_name, []).append(pane)

    sessions: list[TmuxSessionInfo] = []
    for session_name, rows in grouped.items():
        live_rows = [row for row in rows if not row.pane_dead] or rows
        active = next((row for row in live_rows if row.pane_active), live_rows[0])
        sessions.append(
            TmuxSessionInfo(
                session_name=session_name,
                session_id=active.session_id,
                attached=any(row.session_attached for row in rows),
                pane_count=len(rows),
                active_pane=active,
            )
        )
    return sorted(sessions, key=lambda item: item.session_name.casefold())


def discover_tmux_sessions(backend: TmuxPaneLister) -> list[TmuxSessionInfo]:
    if not backend.available:
        raise TmuxError("tmux is not installed or not found on PATH")
    return group_tmux_sessions(parse_tmux_panes(backend.list_panes_text(TMUX_PANE_FORMAT)))


def _next_tab_index(store: SessionStore, window_id: str) -> int:
    records = store.list(include_archived=True, window_id=window_id)
    return max((record.tab_index for record in records), default=-1) + 1


def _record_from_session(
    session: TmuxSessionInfo,
    *,
    tag: str,
    window_id: str,
    window_title: str,
    tab_index: int,
    visible: bool,
) -> SessionRecord:
    now = utc_now()
    return SessionRecord(
        id=short_id("td"),
        name=session.session_name,
        tag=tag or "adopted",
        command=session.command,
        cwd=str(Path(session.cwd).expanduser()),
        tmux_name=session.session_name,
        created_at=now,
        last_opened_at=now,
        status="running",
        last_pid=session.pid,
        window_id=window_id or "adopted",
        window_title=window_title or "Adopted tmux",
        tab_index=max(0, int(tab_index)),
        visible=bool(visible),
        pane_target=session.pane_target,
        external=True,
    )


def adopt_tmux_sessions(
    store: SessionStore,
    sessions: Iterable[TmuxSessionInfo],
    *,
    selectors: Iterable[str] = (),
    tag: str = "adopted",
    window_id: str = "adopted",
    window_title: str = "Adopted tmux",
    visible: bool = True,
    include_termdeck: bool = False,
    dry_run: bool = False,
) -> list[AdoptTmuxResult]:
    """Register existing tmux sessions as browser-like t1000 tabs.

    t1000 already owns sessions whose tmux name starts with ``termdeck_``;
    those are skipped by default so repeated adoption does not create loops.
    """

    wanted = {item.strip() for item in selectors if item.strip()}
    results: list[AdoptTmuxResult] = []
    tab_index = _next_tab_index(store, window_id)

    for session in sessions:
        if wanted and session.session_name not in wanted and session.session_id not in wanted:
            continue
        if not include_termdeck and session.session_name.startswith("termdeck_"):
            results.append(
                AdoptTmuxResult(
                    tmux_name=session.session_name,
                    status="skipped",
                    ok=True,
                    command=session.command,
                    cwd=session.cwd,
                    pid=session.pid,
                    pane_target=session.pane_target,
                    pane_count=session.pane_count,
                    reason="already looks like a t1000-managed tmux session",
                )
            )
            continue

        existing = store.get_by_tmux_name(session.session_name)
        if existing:
            updated = SessionRecord(
                **(
                    dict(existing.__dict__)
                    | {
                        "tag": tag or existing.tag,
                        "command": session.command,
                        "cwd": str(Path(session.cwd).expanduser()),
                        "status": "running",
                        "last_pid": session.pid,
                        "window_id": window_id or existing.window_id,
                        "window_title": window_title or existing.window_title,
                        "visible": visible,
                        "pane_target": session.pane_target,
                        "external": True,
                    }
                )
            )
            if not dry_run:
                store.upsert(updated)
            results.append(
                AdoptTmuxResult(
                    tmux_name=session.session_name,
                    status="planned-update" if dry_run else "updated",
                    ok=True,
                    id=updated.id,
                    name=updated.name,
                    tag=updated.tag,
                    command=updated.command,
                    cwd=updated.cwd,
                    pid=updated.last_pid,
                    pane_target=updated.pane_target or "",
                    pane_count=session.pane_count,
                )
            )
            continue

        record = _record_from_session(
            session,
            tag=tag,
            window_id=window_id,
            window_title=window_title,
            tab_index=tab_index,
            visible=visible,
        )
        tab_index += 1
        if not dry_run:
            store.upsert(record)
        results.append(
            AdoptTmuxResult(
                tmux_name=session.session_name,
                status="planned-adopt" if dry_run else "adopted",
                ok=True,
                id=record.id,
                name=record.name,
                tag=record.tag,
                command=record.command,
                cwd=record.cwd,
                pid=record.last_pid,
                pane_target=record.pane_target or "",
                pane_count=session.pane_count,
            )
        )

    if wanted:
        seen = {result.tmux_name for result in results}
        for selector in sorted(wanted - seen):
            results.append(
                AdoptTmuxResult(
                    tmux_name=selector,
                    status="missing",
                    ok=False,
                    reason="no matching tmux session or session id found",
                )
            )
    return results
