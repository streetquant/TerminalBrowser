from __future__ import annotations

import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SessionRecord
from .session_recipe import (
    DEFAULT_RESTART_POLICY,
    SessionRecipeError,
    parse_session_recipe,
    record_recipe_fields,
    recovery_command_specificity,
)

APP_DIR = Path.home() / ".local" / "share" / "termdeck"
DEFAULT_DB_PATH = APP_DIR / "sessions.sqlite3"
DEFAULT_AUTH_ROOT = APP_DIR / "terminal-auth"

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")



def utc_now() -> str:
    # Browser-style close/reopen ordering needs more than second precision:
    # several tabs can be closed in one keyboard chord burst.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        auth_root: str | Path | None = None,
        account_source: str | Path | None = None,
        provision_auth_scopes: bool | None = None,
    ):
        self.path = Path(path).expanduser() if path else DEFAULT_DB_PATH
        self.auth_root = (
            Path(auth_root).expanduser()
            if auth_root is not None
            else (DEFAULT_AUTH_ROOT if path is None else self.path.parent / "terminal-auth")
        )
        # Account isolation is an optional integration, not a prerequisite for
        # creating, opening, or recovering a terminal.  Explicit auth paths keep
        # the historical opt-in behavior for account-specific callers/tests; the
        # normal GUI/store path remains completely tool-agnostic.
        self._account_source_override = (
            Path(account_source).expanduser()
            if account_source is not None
            else None
        )
        configured_source = os.environ.get("TERMDECK_CODEX_ACCOUNT_SOURCE", "").strip()
        self.account_source = self._account_source_override or (
            Path(configured_source).expanduser()
            if configured_source
            else Path.home() / ".codex"
        )
        self.provision_auth_scopes = (
            bool(auth_root is not None or account_source is not None)
            if provision_auth_scopes is None
            else bool(provision_auth_scopes)
        )
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.provision_auth_scopes:
            self.auth_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.auth_root.chmod(0o700)
        # Keep the GUI responsive if a second TermDeck/CLI process briefly
        # holds the registry, and avoid a full fsync for every tab-state update.
        self.conn = sqlite3.connect(str(self.path), timeout=1.0)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA busy_timeout = 1000")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        if self.path == Path(":memory:"):
            return
        try:
            mode = self.conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode and str(mode[0]).casefold() == "wal":
                # WAL + NORMAL preserves database consistency while avoiding
                # a storage flush on every small UI metadata transaction.
                self.conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            # Some unusual filesystems cannot host WAL sidecars. SQLite's
            # default journal remains a safe compatibility fallback.
            pass

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
                external INTEGER NOT NULL DEFAULT 0,
                recovery_command TEXT,
                recovery_cwd TEXT,
                auth_scope_id TEXT,
                codex_home TEXT,
                selected_account TEXT,
                pending_account TEXT,
                auth_restart_required INTEGER NOT NULL DEFAULT 0,
                auth_changed_at TEXT,
                recovery_recipe TEXT,
                recovery_recipe_sha256 TEXT,
                restart_policy TEXT NOT NULL DEFAULT 'on-failure',
                recovery_attempts INTEGER NOT NULL DEFAULT 0,
                recovery_last_attempt_at TEXT,
                recovery_last_error TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS window_captains (
                window_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remembered_windows (
                window_id TEXT PRIMARY KEY,
                window_title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL
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
        self._ensure_column("recovery_command", "TEXT")
        self._ensure_column("recovery_cwd", "TEXT")
        self._ensure_column("auth_scope_id", "TEXT")
        self._ensure_column("codex_home", "TEXT")
        self._ensure_column("selected_account", "TEXT")
        self._ensure_column("pending_account", "TEXT")
        self._ensure_column("auth_restart_required", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("auth_changed_at", "TEXT")
        self._ensure_column("recovery_recipe", "TEXT")
        self._ensure_column("recovery_recipe_sha256", "TEXT")
        self._ensure_column(
            "restart_policy",
            "TEXT NOT NULL DEFAULT 'on-failure'",
        )
        self._ensure_column("recovery_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("recovery_last_attempt_at", "TEXT")
        self._ensure_column("recovery_last_error", "TEXT")
        self._clear_managed_server_local_pane_targets()
        self._backfill_recovery_recipes()
        if self.provision_auth_scopes:
            self._ensure_auth_scopes()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tag ON sessions(tag)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_window ON sessions(window_id, tab_index)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_visible ON sessions(visible)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pinned ON sessions(pinned)")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_auth_scope ON sessions(auth_scope_id) "
            "WHERE auth_scope_id IS NOT NULL AND auth_scope_id != ''"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_codex_home ON sessions(codex_home) "
            "WHERE codex_home IS NOT NULL AND codex_home != ''"
        )
        self._bootstrap_remembered_windows()
        self._cleanup_stale_window_captains()
        self.conn.commit()

    def _ensure_column(self, name: str, ddl: str) -> None:
        rows = self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        existing = {str(row[1]) for row in rows}
        if name not in existing:
            self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")

    def _clear_managed_server_local_pane_targets(self) -> int:
        """Remove tmux-server-local pane IDs from durable managed records.

        Explicit ``%pane`` targets are valid for adopted external sessions, but
        tmux allocates new IDs after a server restart.  Managed rows are addressed
        by their stable session name and must never carry an old generation's ID.
        The migration is idempotent and intentionally leaves external rows intact.
        """

        cursor = self.conn.execute(
            """
            UPDATE sessions
            SET pane_target = NULL
            WHERE external = 0 AND pane_target IS NOT NULL
            """
        )
        return max(0, int(cursor.rowcount))

    def _bootstrap_remembered_windows(self) -> int:
        """Add only currently open legacy windows to the independent registry.

        Older databases inferred window existence from visible tab rows.  Do
        not resurrect every historical all-hidden group during migration; only
        groups that were still marked open become durable windows.
        """

        rows = self.conn.execute(
            """
            SELECT window_id, window_title, MIN(created_at), MAX(last_opened_at)
            FROM sessions
            WHERE visible = 1 AND status != 'archived'
            GROUP BY window_id, window_title
            """
        ).fetchall()
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO remembered_windows (
                window_id, window_title, created_at, last_opened_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    str(row["window_id"]),
                    str(row["window_title"] or "Window"),
                    str(row[2] or utc_now()),
                    str(row[3] or utc_now()),
                )
                for row in rows
            ],
        )
        return max(0, self.conn.total_changes - before)

    def remember_window(self, window_id: str, window_title: str) -> None:
        """Persist a logical window independently of its current tab states."""

        clean_id = (window_id or "main").strip() or "main"
        clean_title = (window_title or "Window").strip() or "Window"
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO remembered_windows (
                window_id, window_title, created_at, last_opened_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(window_id) DO UPDATE SET
                window_title = excluded.window_title,
                last_opened_at = excluded.last_opened_at
            """,
            (clean_id, clean_title, now, now),
        )
        self.conn.commit()

    def remembered_windows(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT window_id, window_title
            FROM remembered_windows
            ORDER BY window_title COLLATE NOCASE, window_id
            """
        ).fetchall()
        return [(str(row["window_id"]), str(row["window_title"])) for row in rows]

    def forget_window(self, window_id: str) -> bool:
        """Remove an empty logical window from durable restore state.

        Refuse while any session row still belongs to the window.  This keeps a
        partial destructive action (for example, an auth-guarded session that
        could not be stopped) recoverable instead of orphaning it from startup.
        """

        clean_id = (window_id or "main").strip() or "main"
        remaining = self.conn.execute(
            "SELECT 1 FROM sessions WHERE window_id = ? LIMIT 1",
            (clean_id,),
        ).fetchone()
        if remaining is not None:
            return False
        self.conn.execute(
            "DELETE FROM window_captains WHERE window_id = ?",
            (clean_id,),
        )
        self.conn.execute(
            "DELETE FROM remembered_windows WHERE window_id = ?",
            (clean_id,),
        )
        self.conn.commit()
        return True

    def _new_auth_scope(self) -> tuple[str, str, str | None]:
        # Keep the optional account integration out of SessionStore's import and
        # normal initialization path.  A missing third-party executable does not
        # affect the generic session registry.
        from .codex_auth import (
            new_auth_scope_id,
            prepare_auth_scope,
            read_auth_state,
        )

        self.auth_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_root.chmod(0o700)
        scope_id = new_auth_scope_id()
        codex_home = self.auth_root / scope_id
        prepare_auth_scope(
            codex_home,
            account_source=self._account_source_override,
        )
        state = read_auth_state(codex_home, account_source=self.account_source)
        inherited_account = (
            state.selected_account if state.canonical_binding_ready else None
        )
        return scope_id, str(codex_home), inherited_account

    def _ensure_auth_scopes(self) -> None:
        from .codex_auth import prepare_auth_scope

        rows = self.conn.execute(
            "SELECT id, auth_scope_id, codex_home FROM sessions"
        ).fetchall()
        for row in rows:
            scope_id = str(row["auth_scope_id"] or "").strip()
            codex_home = str(row["codex_home"] or "").strip()
            if not scope_id or not codex_home:
                scope_id, codex_home, inherited_account = self._new_auth_scope()
                self.conn.execute(
                    """
                    UPDATE sessions
                    SET auth_scope_id = ?, codex_home = ?, selected_account = ?
                    WHERE id = ?
                    """,
                    (scope_id, codex_home, inherited_account, row["id"]),
                )
            else:
                prepare_auth_scope(
                    codex_home,
                    account_source=self._account_source_override,
                )

    def ensure_auth_scope(self, session_id: str) -> SessionRecord:
        """Provision the optional private account environment for one terminal."""

        current = self.get(session_id)
        if current is None:
            raise KeyError(session_id)
        if current.external:
            raise ValueError("external tmux sessions cannot receive a private auth scope")
        if current.auth_scope_id and current.codex_home:
            return current
        scope_id, codex_home, inherited_account = self._new_auth_scope()
        self.conn.execute(
            """
            UPDATE sessions
            SET auth_scope_id = ?, codex_home = ?, selected_account = ?,
                auth_restart_required = 0
            WHERE id = ? AND (auth_scope_id IS NULL OR auth_scope_id = '')
              AND (codex_home IS NULL OR codex_home = '')
            """,
            (scope_id, codex_home, inherited_account, session_id),
        )
        self.conn.commit()
        refreshed = self.get(session_id)
        if refreshed is None:
            raise KeyError(session_id)
        # A concurrent same-user provisioner may have won; its immutable scope
        # is authoritative.  Remove only our unused empty scope when possible.
        if refreshed.auth_scope_id != scope_id:
            try:
                import shutil

                shutil.rmtree(codex_home)
            except OSError:
                pass
        return refreshed

    @staticmethod
    def _recipe_values_for_record(record: SessionRecord) -> tuple[str | None, str | None, str]:
        encoded = getattr(record, "recovery_recipe", None)
        digest = getattr(record, "recovery_recipe_sha256", None)
        if encoded is not None or digest is not None:
            if encoded is None or digest is None:
                raise SessionRecipeError("session recipe and digest must be stored together")
            recipe = parse_session_recipe(encoded, digest)
            return encoded, digest, recipe.restart_policy
        if bool(getattr(record, "external", False)):
            return None, None, "never"
        return record_recipe_fields(
            command=record.command,
            cwd=record.cwd,
            recovery_command=record.recovery_command,
            recovery_cwd=record.recovery_cwd,
            restart_policy=(
                getattr(record, "restart_policy", DEFAULT_RESTART_POLICY)
                or DEFAULT_RESTART_POLICY
            ),
            origin=(
                "legacy-recovery"
                if (record.recovery_command or "").strip()
                else "launch"
            ),
        )

    def _record_with_recovery_recipe(self, record: SessionRecord) -> SessionRecord:
        encoded, digest, policy = self._recipe_values_for_record(record)
        return replace(
            record,
            recovery_recipe=encoded,
            recovery_recipe_sha256=digest,
            restart_policy=policy,
            recovery_attempts=max(0, int(getattr(record, "recovery_attempts", 0))),
        )

    def _backfill_recovery_recipes(self) -> int:
        rows = self.conn.execute("SELECT * FROM sessions").fetchall()
        updates: list[tuple[str | None, str | None, str, str]] = []
        for row in rows:
            record = self._row_to_record(row)
            try:
                encoded, digest, policy = self._recipe_values_for_record(record)
            except SessionRecipeError:
                # Preserve a malformed pre-existing receipt for diagnostics; do
                # not silently replace its authority with mutable legacy fields.
                if row["recovery_recipe"] is not None or row["recovery_recipe_sha256"] is not None:
                    continue
                encoded = digest = None
                policy = "never"
            if (
                row["recovery_recipe"],
                row["recovery_recipe_sha256"],
                str(row["restart_policy"] or DEFAULT_RESTART_POLICY),
            ) != (encoded, digest, policy):
                updates.append((encoded, digest, policy, str(row["id"])))
        if not updates:
            return 0
        self.conn.executemany(
            """
            UPDATE sessions
            SET recovery_recipe = ?, recovery_recipe_sha256 = ?, restart_policy = ?
            WHERE id = ?
            """,
            updates,
        )
        return len(updates)

    def ensure_recovery_recipe(self, session_id: str) -> SessionRecord:
        current = self.get(session_id)
        if current is None:
            raise KeyError(session_id)
        prepared = self._record_with_recovery_recipe(current)
        if prepared == current:
            return current
        self.upsert(prepared)
        refreshed = self.get(session_id)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

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
        environment: Mapping[str, str] | None = None,
        restart_policy: str = DEFAULT_RESTART_POLICY,
    ) -> SessionRecord:
        sid = short_id("td")
        if self.provision_auth_scopes:
            auth_scope_id, codex_home, inherited_account = self._new_auth_scope()
        else:
            auth_scope_id = codex_home = inherited_account = None
        cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
        now = utc_now()
        safe_tag = tmux_safe_name(tag or "default")
        safe_name = tmux_safe_name(name or command or "shell")
        tmux_name = tmux_safe_name(f"termdeck_{safe_tag}_{safe_name}_{sid.replace('-', '_')}")
        recipe_json, recipe_sha256, clean_restart_policy = record_recipe_fields(
            command=command or os.environ.get("SHELL", "/bin/sh"),
            cwd=cwd,
            environment=environment,
            restart_policy=restart_policy,
            origin="launch",
        )
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
            recovery_command=None,
            recovery_cwd=None,
            auth_scope_id=auth_scope_id,
            codex_home=codex_home,
            selected_account=inherited_account,
            pending_account=None,
            auth_restart_required=False,
            auth_changed_at=None,
            recovery_recipe=recipe_json,
            recovery_recipe_sha256=recipe_sha256,
            restart_policy=clean_restart_policy,
            recovery_attempts=0,
            recovery_last_attempt_at=None,
            recovery_last_error=None,
        )
        self.upsert(record)
        return record

    def upsert(self, record: SessionRecord) -> None:
        existing = self.get(record.id)
        if existing is not None and existing.auth_scope_id and existing.codex_home:
            record = replace(
                record,
                auth_scope_id=existing.auth_scope_id,
                codex_home=existing.codex_home,
            )
        elif record.auth_scope_id and record.codex_home:
            from .codex_auth import prepare_auth_scope

            prepare_auth_scope(
                record.codex_home,
                account_source=self._account_source_override,
            )
        elif self.provision_auth_scopes:
            auth_scope_id, codex_home, inherited_account = self._new_auth_scope()
            record = replace(
                record,
                auth_scope_id=auth_scope_id,
                codex_home=codex_home,
                selected_account=inherited_account,
                auth_restart_required=False,
            )
        else:
            record = replace(record, auth_scope_id=None, codex_home=None)
        record = self._record_with_recovery_recipe(record)
        if not bool(getattr(record, "external", False)) and record.pane_target is not None:
            record = replace(record, pane_target=None)
        payload = dict(record.__dict__)
        payload["visible"] = 1 if record.visible else 0
        payload["pinned"] = 1 if getattr(record, "pinned", False) else 0
        payload["external"] = 1 if getattr(record, "external", False) else 0
        payload["auth_restart_required"] = 1 if getattr(record, "auth_restart_required", False) else 0
        payload["recovery_attempts"] = max(0, int(getattr(record, "recovery_attempts", 0)))
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, name, tag, command, cwd, tmux_name, created_at, last_opened_at,
                status, last_pid, window_id, window_title, tab_index, visible, pane_target, external, pinned,
                recovery_command, recovery_cwd, auth_scope_id, codex_home, selected_account,
                pending_account, auth_restart_required, auth_changed_at,
                recovery_recipe, recovery_recipe_sha256, restart_policy, recovery_attempts,
                recovery_last_attempt_at, recovery_last_error
            )
            VALUES (
                :id, :name, :tag, :command, :cwd, :tmux_name, :created_at, :last_opened_at,
                :status, :last_pid, :window_id, :window_title, :tab_index, :visible, :pane_target, :external, :pinned,
                :recovery_command, :recovery_cwd, :auth_scope_id, :codex_home, :selected_account,
                :pending_account, :auth_restart_required, :auth_changed_at,
                :recovery_recipe, :recovery_recipe_sha256, :restart_policy, :recovery_attempts,
                :recovery_last_attempt_at, :recovery_last_error
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
                external = excluded.external,
                recovery_command = excluded.recovery_command,
                recovery_cwd = excluded.recovery_cwd,
                selected_account = excluded.selected_account,
                pending_account = excluded.pending_account,
                auth_restart_required = excluded.auth_restart_required,
                auth_changed_at = excluded.auth_changed_at,
                recovery_recipe = excluded.recovery_recipe,
                recovery_recipe_sha256 = excluded.recovery_recipe_sha256,
                restart_policy = excluded.restart_policy,
                recovery_attempts = excluded.recovery_attempts,
                recovery_last_attempt_at = excluded.recovery_last_attempt_at,
                recovery_last_error = excluded.recovery_last_error
            """,
            payload,
        )
        # A tab moved to another window cannot remain captain of its previous
        # window. Keep this invariant in the store instead of relying on every
        # lifecycle or UI caller to remember the cleanup.
        self.conn.execute(
            "DELETE FROM window_captains WHERE session_id = ? AND window_id != ?",
            (record.id, record.window_id),
        )
        if record.visible and record.status.casefold() != "archived":
            now = utc_now()
            self.conn.execute(
                """
                INSERT INTO remembered_windows (
                    window_id, window_title, created_at, last_opened_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(window_id) DO UPDATE SET
                    window_title = excluded.window_title,
                    last_opened_at = excluded.last_opened_at
                """,
                (record.window_id, record.window_title or "Window", now, now),
            )
        self.conn.commit()

    def update(self, session_id: str, **changes: object) -> SessionRecord:
        current = self.get(session_id)
        if not current:
            raise KeyError(session_id)
        for field in ("auth_scope_id", "codex_home"):
            if field in changes and changes[field] != getattr(current, field):
                raise ValueError(f"{field} is immutable for terminal {session_id}")
        data: dict[str, Any] = dict(current.__dict__)
        data.update(changes)
        recipe_inputs = {
            "command",
            "cwd",
            "recovery_command",
            "recovery_cwd",
            "restart_policy",
        }
        if recipe_inputs.intersection(changes) and not {
            "recovery_recipe",
            "recovery_recipe_sha256",
        }.intersection(changes):
            data["recovery_recipe"] = None
            data["recovery_recipe_sha256"] = None
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
        if not bool(data.get("external", False)):
            data["pane_target"] = None
        if "auth_restart_required" in data:
            data["auth_restart_required"] = bool(data["auth_restart_required"])
        if "recovery_attempts" in data:
            data["recovery_attempts"] = max(0, int(data["recovery_attempts"]))
        updated = self._record_with_recovery_recipe(SessionRecord(**data))
        self.upsert(updated)
        return updated

    def update_runtime_states(
        self,
        updates: Iterable[tuple[str, str, bool, int | None]],
    ) -> int:
        """Apply reconciliation state changes without reordering browser history."""

        rows = list(updates)
        if not rows:
            return 0
        self.conn.executemany(
            """
            UPDATE sessions
            SET status = ?, visible = ?, last_pid = ?
            WHERE id = ?
            """,
            [
                (status, 1 if visible else 0, last_pid, session_id)
                for session_id, status, visible, last_pid in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def update_recovery_commands(
        self,
        updates: Iterable[tuple[str, str | None]],
    ) -> int:
        """Compatibility helper for callers without recovery cwd metadata."""

        return self.update_recovery_metadata(
            (session_id, command, None) for session_id, command in updates
        )

    def update_recovery_metadata(
        self,
        updates: Iterable[tuple[str, str | None, str | None]],
    ) -> int:
        """Persist changed crash-relaunch commands and cwd values atomically."""

        requested = [
            (session_id, command, cwd if command else None)
            for session_id, command, cwd in updates
        ]
        if not requested:
            return 0
        identifiers = [session_id for session_id, _command, _cwd in requested]
        placeholders = ",".join("?" for _ in identifiers)
        existing = {
            str(row["id"]): (row["recovery_command"], row["recovery_cwd"])
            for row in self.conn.execute(
                f"SELECT id, recovery_command, recovery_cwd FROM sessions WHERE id IN ({placeholders})",
                identifiers,
            ).fetchall()
        }
        rows = []
        for session_id, command, cwd in requested:
            previous_command, previous_cwd = existing.get(session_id, (None, None))
            if (
                command is not None
                and recovery_command_specificity(previous_command)
                > recovery_command_specificity(command)
            ):
                # Preserve any application's more-specific opaque resume target
                # over a weaker bare-launcher observation. A positive None
                # observation still clears metadata after the process ended.
                rows.append((session_id, previous_command, previous_cwd))
            else:
                rows.append((session_id, command, cwd))
        before = self.conn.total_changes
        self.conn.executemany(
            """
            UPDATE sessions
            SET recovery_command = ?, recovery_cwd = ?
            WHERE id = ? AND external = 0
              AND (recovery_command IS NOT ? OR recovery_cwd IS NOT ?)
            """,
            [
                (command, cwd, session_id, command, cwd)
                for session_id, command, cwd in rows
            ],
        )
        changed = max(0, self.conn.total_changes - before)
        if changed:
            refresh_rows = self.conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders}) AND external = 0",
                identifiers,
            ).fetchall()
            recipe_updates = []
            for row in refresh_rows:
                record = replace(
                    self._row_to_record(row),
                    recovery_recipe=None,
                    recovery_recipe_sha256=None,
                )
                prepared = self._record_with_recovery_recipe(record)
                recipe_updates.append(
                    (
                        prepared.recovery_recipe,
                        prepared.recovery_recipe_sha256,
                        prepared.restart_policy,
                        prepared.id,
                    )
                )
            self.conn.executemany(
                """
                UPDATE sessions
                SET recovery_recipe = ?, recovery_recipe_sha256 = ?, restart_policy = ?
                WHERE id = ?
                """,
                recipe_updates,
            )
        self.conn.commit()
        return changed

    def get(self, session_id: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_tmux_name(self, tmux_name: str) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE tmux_name = ?", (tmux_name,)).fetchone()
        return self._row_to_record(row) if row else None

    def set_window_captain(self, window_id: str, session_id: str) -> SessionRecord:
        """Designate one session as the captain of its current window."""

        record = self.get(session_id)
        if record is None:
            raise KeyError(session_id)
        if record.window_id != window_id:
            raise ValueError(
                f"session {session_id} belongs to window {record.window_id!r}, not {window_id!r}"
            )
        # The UNIQUE session_id constraint prevents one tab from captaining two
        # windows. Delete any stale prior assignment before the window upsert.
        self.conn.execute("DELETE FROM window_captains WHERE session_id = ?", (session_id,))
        self.conn.execute(
            """
            INSERT INTO window_captains (window_id, session_id)
            VALUES (?, ?)
            ON CONFLICT(window_id) DO UPDATE SET session_id = excluded.session_id
            """,
            (window_id, session_id),
        )
        self.conn.commit()
        return record

    def captain_for_window(self, window_id: str) -> SessionRecord | None:
        """Return a window's captain, removing a missing or moved assignment."""

        row = self.conn.execute(
            "SELECT session_id FROM window_captains WHERE window_id = ?",
            (window_id,),
        ).fetchone()
        if row is None:
            return None
        session_id = str(row["session_id"])
        record = self.get(session_id)
        if record is None or record.window_id != window_id:
            self.clear_window_captain(window_id, session_id=session_id)
            return None
        return record

    def clear_window_captain(self, window_id: str, session_id: str | None = None) -> bool:
        """Clear a captain, optionally only when it still matches ``session_id``."""

        if session_id is None:
            cursor = self.conn.execute(
                "DELETE FROM window_captains WHERE window_id = ?",
                (window_id,),
            )
        else:
            cursor = self.conn.execute(
                "DELETE FROM window_captains WHERE window_id = ? AND session_id = ?",
                (window_id, session_id),
            )
        self.conn.commit()
        return int(cursor.rowcount) > 0

    def _cleanup_stale_window_captains(self) -> int:
        cursor = self.conn.execute(
            """
            DELETE FROM window_captains
            WHERE NOT EXISTS (
                SELECT 1
                FROM sessions
                WHERE sessions.id = window_captains.session_id
                  AND sessions.window_id = window_captains.window_id
            )
            """
        )
        return max(0, int(cursor.rowcount))

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

    def restore_candidates(
        self,
        limit: int | None = None,
        *,
        window_id: str | None = None,
    ) -> list[SessionRecord]:
        """Return every visible restorable tab, optionally bounded by callers.

        Browser restoration must never silently discard tab 13 or later. The
        unbounded default is therefore authoritative for GUI/startup recovery;
        bounded callers may still request a positive prefix for previews or
        diagnostics. When ``window_id`` is set, rows remain scoped to that
        logical window so restore cannot steal tabs from another window.
        """

        rows = self.list(visible_only=True, window_id=window_id, include_stopped=False)
        if limit is None:
            return rows
        clean_limit = max(1, int(limit))
        return rows[:clean_limit]

    def rename_window(self, window_id: str, title: str) -> int:
        """Rename one logical TermDeck window without changing tab timestamps."""

        clean_title = title.strip() or "Window"
        cursor = self.conn.execute(
            "UPDATE sessions SET window_title = ? WHERE window_id = ?",
            (clean_title, window_id),
        )
        self.conn.execute(
            """
            UPDATE remembered_windows
            SET window_title = ?, last_opened_at = ?
            WHERE window_id = ?
            """,
            (clean_title, utc_now(), window_id),
        )
        self.conn.commit()
        return max(0, int(cursor.rowcount))

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

    def windows_for_restore(self) -> list[tuple[str, str, list[SessionRecord]]]:
        """Return every durable window, including windows with no live tabs."""

        records = self.visible_records()
        grouped: dict[str, list[SessionRecord]] = {}
        titles: dict[str, str] = {}
        for record in records:
            grouped.setdefault(record.window_id, []).append(record)
            titles.setdefault(record.window_id, record.window_title)

        remembered = self.remembered_windows()
        known_ids = {window_id for window_id, _title in remembered}
        # Defensive compatibility for a database written by another process
        # between schema initialization and this read.
        remembered.extend(
            (window_id, titles.get(window_id, "Window"))
            for window_id in grouped
            if window_id not in known_ids
        )
        remembered.sort(key=lambda item: (item[1].casefold(), item[0]))

        result: list[tuple[str, str, list[SessionRecord]]] = []
        for window_id, window_title in remembered:
            rows = sorted(
                grouped.get(window_id, []),
                key=lambda row: (not row.pinned, row.tab_index, row.last_opened_at),
            )
            result.append((window_id, window_title, rows))
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
        self.conn.execute("DELETE FROM window_captains WHERE session_id = ?", (session_id,))
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
            pane_target=row["pane_target"] if bool(row["external"]) else None,
            external=bool(row["external"]),
            recovery_command=row["recovery_command"],
            recovery_cwd=row["recovery_cwd"],
            auth_scope_id=row["auth_scope_id"],
            codex_home=row["codex_home"],
            selected_account=row["selected_account"],
            pending_account=row["pending_account"],
            auth_restart_required=bool(row["auth_restart_required"]),
            auth_changed_at=row["auth_changed_at"],
            recovery_recipe=row["recovery_recipe"],
            recovery_recipe_sha256=row["recovery_recipe_sha256"],
            restart_policy=str(row["restart_policy"] or DEFAULT_RESTART_POLICY),
            recovery_attempts=max(0, int(row["recovery_attempts"] or 0)),
            recovery_last_attempt_at=row["recovery_last_attempt_at"],
            recovery_last_error=row["recovery_last_error"],
        )
