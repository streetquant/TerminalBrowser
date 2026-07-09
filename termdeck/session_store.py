from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SessionRecord

APP_DIR = Path.home() / ".local" / "share" / "termdeck"
DEFAULT_DB_PATH = APP_DIR / "sessions.sqlite3"

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_id(prefix: str = "td") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def tmux_safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "session"
    if cleaned[0].isdigit():
        cleaned = f"s_{cleaned}"
    return cleaned[:96]


class SessionStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else DEFAULT_DB_PATH
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tag TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                tmux_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                last_pid INTEGER,
                window_id TEXT NOT NULL DEFAULT 'main',
                window_title TEXT NOT NULL DEFAULT 'Main',
                tab_index INTEGER NOT NULL DEFAULT 0,
                visible INTEGER NOT NULL DEFAULT 1,
                pinned INTEGER NOT NULL DEFAULT 0,
                pane_target TEXT,
                external INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._ensure_column("window_id", "TEXT NOT NULL DEFAULT 'main'")
        self._ensure_column("window_title", "TEXT NOT NULL DEFAULT 'Main'")
        self._ensure_column("tab_index", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("visible", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("pinned", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("pane_target", "TEXT")
        self._ensure_column("external", "INTEGER NOT NULL DEFAULT 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tag ON sessions(tag)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_window ON sessions(window_id, tab_index)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_visible ON sessions(visible)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pinned ON sessions(pinned)")
        self.conn.commit()

    def _ensure_column(self, name: str, ddl: str) -> None:
        rows = self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        existing = {str(row[1]) for row in rows}
        if name not in existing:
            self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")

    def create(
        self,
        name: str,
        tag: str,
        command: str,
        cwd: str | None = None,
        *,
        window_id: str = "main",
        window_title: str = "Main",
        tab_index: int = 0,
        visible: bool = True,
        pinned: bool = False,
    ) -> SessionRecord:
        sid = short_id("td")
        cwd = str(Path(cwd or os.getcwd()).expanduser())
        now = utc_now()
        safe_tag = tmux_safe_name(tag or "default")
        safe_name = tmux_safe_name(name or command or "shell")
        tmux_name = tmux_safe_name(f"termdeck_{safe_tag}_{safe_name}_{sid.replace('-', '_')}")
        record = SessionRecord(
            id=sid,
            name=name or command or "shell",
            tag=tag or "default",
            command=command or os.environ.get("SHELL", "/bin/sh"),
            cwd=cwd,
            tmux_name=tmux_name,
            created_at=now,
            last_opened_at=now,
            status="new",
            last_pid=None,
            window_id=window_id or "main",
            window_title=window_title or "Main",
            tab_index=max(0, int(tab_index)),
            visible=bool(visible),
            pinned=bool(pinned),
            pane_target=None,
            external=False,
        )
        self.upsert(record)
        return record

    def upsert(self, record: SessionRecord) -> None:
        payload = dict(record.__dict__)
        payload["visible"] = 1 if record.visible else 0
        payload["pinned"] = 1 if getattr(record, "pinned", False) else 0
        payload["external"] = 1 if getattr(record, "external", False) else 0
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, name, tag, command, cwd, tmux_name, created_at, last_opened_at,
                status, last_pid, window_id, window_title, tab_index, visible, pane_target, external, pinned
            )
            VALUES (
                :id, :name, :tag, :command, :cwd, :tmux_name, :created_at, :last_opened_at,
                :status, :last_pid, :window_id, :window_title, :tab_index, :visible, :pane_target, :external, :pinned
            )
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                tag = excluded.tag,
                command = excluded.command,
                cwd = excluded.cwd,
                tmux_name = excluded.tmux_name,
                last_opened_at = excluded.last_opened_at,
                status = excluded.status,
                last_pid = excluded.last_pid,
                window_id = excluded.window_id,
                window_title = excluded.window_title,
                tab_index = excluded.tab_index,
                visible = excluded.visible,
                pinned = excluded.pinned,
                pane_target = excluded.pane_target,
                external = excluded.external
            """,
            payload,
        )
        self.conn.commit()

    def update(self, session_id: str, **changes: object) -> SessionRecord:
        current = self.get(session_id)
        if not current:
            raise KeyError(session_id)
        data: dict[str, Any] = dict(current.__dict__)
        data.update(changes)
        if "last_opened_at" not in changes:
            data["last_opened_at"] = utc_now()
        if "visible" in data:
            data["visible"] = bool(data["visible"])
        if "tab_index" in data:
            data["tab_index"] = max(0, int(data["tab_index"]))
        if "pinned" in data:
            data["pinned"] = bool(data["pinned"])
        if "external" in data:
            data["external"] = bool(data["external"])
        updated = SessionRecord(**data)
        self.upsert(updated)
        return updated

    def get(self, session_id: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_tmux_name(self, tmux_name: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE tmux_name = ?", (tmux_name,)).fetchone()
        return self._row_to_record(row) if row else None

    def list(
        self,
        include_archived: bool = False,
        *,
        visible_only: bool = False,
        window_id: str | None = None,
        include_stopped: bool = True,
    ) -> list[SessionRecord]:
        where: list[str] = []
        params: list[object] = []
        if not include_archived:
            where.append("status != 'archived'")
        if visible_only:
            where.append("visible = 1")
        if not include_stopped:
            where.append("status != 'stopped'")
        if window_id is not None:
            where.append("window_id = ?")
            params.append(window_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.conn.execute(
            f"SELECT * FROM sessions {clause} ORDER BY window_title, window_id, pinned DESC, tab_index, last_opened_at DESC",
            params,
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def visible_records(self) -> list[SessionRecord]:
        return self.list(visible_only=True, include_stopped=False)

    def hidden_records(
        self,
        *,
        limit: int = 20,
        tag: str | None = None,
        include_archived: bool = False,
        include_stopped: bool = False,
    ) -> list[SessionRecord]:
        """Return recently hidden browser tabs without exposing process plumbing."""

        clean_limit = max(1, int(limit))
        where = ["visible = 0"]
        params: list[object] = []
        if not include_archived:
            where.append("status != 'archived'")
        if not include_stopped:
            where.append("status != 'stopped'")
        if tag:
            where.append("tag = ?")
            params.append(tag)
        clause = " AND ".join(where)
        rows = self.conn.execute(
            f"""
            SELECT * FROM sessions
            WHERE {clause}
            ORDER BY last_opened_at DESC, created_at DESC, name ASC
            LIMIT ?
            """,
            (*params, clean_limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def restore_candidates(self, limit: int = 12, *, window_id: str | None = None) -> list[SessionRecord]:
        """Return sessions that should be reattached on app/window startup."""

        clean_limit = max(1, int(limit))
        rows = self.list(visible_only=True, window_id=window_id, include_stopped=False)
        if not rows and window_id is not None:
            rows = self.list(visible_only=True, include_stopped=False)
        return rows[:clean_limit]

    def windows_with_visible_sessions(self) -> list[tuple[str, str, list[SessionRecord]]]:
        records = self.visible_records()
        grouped: dict[str, list[SessionRecord]] = {}
        titles: dict[str, str] = {}
        for record in records:
            grouped.setdefault(record.window_id, []).append(record)
            titles.setdefault(record.window_id, record.window_title)
        result: list[tuple[str, str, list[SessionRecord]]] = []
        for window_id in sorted(grouped, key=lambda wid: (titles.get(wid, ""), wid)):
            rows = sorted(grouped[window_id], key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at))
            result.append((window_id, titles.get(window_id, "Window"), rows))
        return result

    def find_equivalent(
        self,
        *,
        name: str,
        tag: str,
        command: str,
        cwd: str,
        window_title: str | None = None,
    ) -> SessionRecord | None:
        """Find a remembered session matching a workspace recipe tab."""

        rows = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE name = ? AND tag = ? AND command = ? AND cwd = ?
              AND status != 'archived'
            ORDER BY CASE WHEN window_title = ? THEN 0 ELSE 1 END, pinned DESC, last_opened_at DESC
            LIMIT 1
            """,
            (name, tag, command, str(Path(cwd).expanduser()), window_title or ""),
        ).fetchall()
        return self._row_to_record(rows[0]) if rows else None

    def tags(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT tag FROM sessions WHERE status != 'archived' ORDER BY tag").fetchall()
        return [str(row[0]) for row in rows]

    def delete(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            name=row["name"],
            tag=row["tag"],
            command=row["command"],
            cwd=row["cwd"],
            tmux_name=row["tmux_name"],
            created_at=row["created_at"],
            last_opened_at=row["last_opened_at"],
            status=row["status"],
            last_pid=row["last_pid"],
            window_id=row["window_id"],
            window_title=row["window_title"],
            tab_index=int(row["tab_index"]),
            visible=bool(row["visible"]),
            pinned=bool(row["pinned"]),
            pane_target=row["pane_target"],
            external=bool(row["external"]),
        )
