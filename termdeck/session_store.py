from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    RECOVERY_INTENT_ACTIVE,
    RECOVERY_INTENT_INACTIVE,
    RECOVERY_INTENT_LEGACY,
    RECOVERY_INTENTS,
    SessionRecord,
)
from .session_recipe import (
    DEFAULT_RESTART_POLICY,
    SessionRecipeError,
    parse_session_recipe,
    record_recipe_fields,
    recovery_command_specificity,
)

_LEGACY_APP_DIR = Path.home() / ".local" / "share" / "termdeck"


def legacy_state_dir() -> Path:
    return Path.home() / ".local" / "share" / "termdeck"


def managed_state_dir(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the mutable TermDeck state directory for the active launcher.

    Managed T1000 launches place their root on the SSD fabric.  Keeping the
    TermDeck directory beside that root makes the session registry and its
    private auth scopes follow the managed state volume without moving the
    user's legacy auth tree.
    """

    values = os.environ if environ is None else environ
    raw_root = str(values.get("T1000_MANAGED_ROOT", "") or "").strip()
    if not raw_root:
        return _LEGACY_APP_DIR
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise ValueError("T1000_MANAGED_ROOT must be an absolute path")
    return root.parent / "termdeck"


def default_db_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return managed_state_dir(environ=environ) / "sessions.sqlite3"


def default_auth_root(*, environ: Mapping[str, str] | None = None) -> Path:
    return managed_state_dir(environ=environ) / "terminal-auth"


APP_DIR = managed_state_dir()
DEFAULT_DB_PATH = default_db_path()
DEFAULT_AUTH_ROOT = default_auth_root()


class SessionDatabaseMigrationError(RuntimeError):
    """Raised when a legacy session database cannot be migrated safely."""


class SessionDatabaseMigrationInProgress(SessionDatabaseMigrationError):
    """Raised when another primary process owns the migration transaction."""


@dataclass(frozen=True)
class SessionDatabaseMigration:
    status: str
    source: Path
    destination: Path
    source_sha256: str | None = None
    destination_sha256: str | None = None

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")

_ACTIVE_RECOVERY_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending"}
)
_INACTIVE_RECOVERY_STATUSES = frozenset(
    {"new", "remembered", "stopped", "archived"}
)


def normalise_recovery_intent(value: object) -> str:
    """Return the canonical persisted recovery-intent value.

    Unknown values become ``legacy`` so malformed historical state cannot
    authorize a startup relaunch.  The spelling remains British to match the
    existing TermDeck normalisation helpers.
    """

    if isinstance(value, bool):
        return RECOVERY_INTENT_ACTIVE if value else RECOVERY_INTENT_INACTIVE
    normalized = str(value or "").strip().casefold()
    aliases = {
        "active": RECOVERY_INTENT_ACTIVE,
        "eligible": RECOVERY_INTENT_ACTIVE,
        "recover": RECOVERY_INTENT_ACTIVE,
        "recovery": RECOVERY_INTENT_ACTIVE,
        "resume": RECOVERY_INTENT_ACTIVE,
        "running": RECOVERY_INTENT_ACTIVE,
        "true": RECOVERY_INTENT_ACTIVE,
        "yes": RECOVERY_INTENT_ACTIVE,
        "1": RECOVERY_INTENT_ACTIVE,
        "inactive": RECOVERY_INTENT_INACTIVE,
        "archived": RECOVERY_INTENT_INACTIVE,
        "manual": RECOVERY_INTENT_INACTIVE,
        "never": RECOVERY_INTENT_INACTIVE,
        "stopped": RECOVERY_INTENT_INACTIVE,
        "terminated": RECOVERY_INTENT_INACTIVE,
        "false": RECOVERY_INTENT_INACTIVE,
        "no": RECOVERY_INTENT_INACTIVE,
        "0": RECOVERY_INTENT_INACTIVE,
        "legacy": RECOVERY_INTENT_LEGACY,
    }
    return aliases.get(normalized, RECOVERY_INTENT_LEGACY)


def recovery_intent_for_status(status: object) -> str:
    """Infer safe compatibility intent for a status-only legacy row."""

    normalized = str(status or "").strip().casefold()
    if normalized in _ACTIVE_RECOVERY_STATUSES:
        return RECOVERY_INTENT_ACTIVE
    if normalized in _INACTIVE_RECOVERY_STATUSES:
        return RECOVERY_INTENT_INACTIVE
    # A start error does not prove that a task ever became active.  Keep it
    # ambiguous until an explicit successful launch or recovery intent exists.
    return RECOVERY_INTENT_LEGACY



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


def _migration_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate == Path(":memory:"):
        raise SessionDatabaseMigrationError(f"{label} cannot be an in-memory database")
    try:
        if candidate.is_symlink():
            raise SessionDatabaseMigrationError(f"{label} must not be a symlink")
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SessionDatabaseMigrationError(f"cannot resolve {label}") from exc
    if resolved.exists():
        try:
            mode = resolved.lstat().st_mode
        except OSError as exc:
            raise SessionDatabaseMigrationError(f"cannot inspect {label}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise SessionDatabaseMigrationError(f"{label} must be a regular file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _harden_regular_file(path: Path, mode: int) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SessionDatabaseMigrationError(f"database sidecar is not a regular file: {path}")
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _harden_database_permissions(path: Path) -> None:
    _harden_regular_file(path, 0o600)
    _harden_regular_file(path.with_name(f"{path.name}-wal"), 0o600)
    _harden_regular_file(path.with_name(f"{path.name}-shm"), 0o600)


def _is_managed_state_path(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(
            managed_state_dir().resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _check_sqlite_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise SessionDatabaseMigrationError(
            f"{path} is not a readable SQLite database"
        ) from exc
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).casefold() != "ok":
            detail = str(result[0]) if result else "no result"
            raise SessionDatabaseMigrationError(
                f"SQLite integrity check failed for {path}: {detail}"
            )
    except sqlite3.Error as exc:
        raise SessionDatabaseMigrationError(
            f"SQLite integrity check failed for {path}"
        ) from exc
    finally:
        connection.close()


def migrate_legacy_database(
    destination: str | Path | None = None,
    *,
    legacy_path: str | Path | None = None,
) -> SessionDatabaseMigration:
    """Copy only the legacy SQLite registry into the managed SSD state.

    Migration is explicit and never runs from ``SessionStore`` construction.
    It does not inspect or copy ``terminal-auth`` or any other legacy state.
    An existing destination is validated and left untouched, making retries
    safe after interruption.
    """

    source = _migration_file(
        legacy_path
        if legacy_path is not None
        else legacy_state_dir() / "sessions.sqlite3",
        label="legacy session database",
    )
    target = _migration_file(
        destination if destination is not None else default_db_path(),
        label="managed session database",
    )
    if source == target:
        if not source.exists():
            return SessionDatabaseMigration("already_current", source, target)
        _check_sqlite_database(source)
        digest = _sha256_file(source)
        return SessionDatabaseMigration("already_current", source, target, digest, digest)
    if not source.exists():
        return SessionDatabaseMigration("source_missing", source, target)

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    lock_path = target.with_name(f".{target.name}.migration.lock")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SessionDatabaseMigrationError("cannot create migration lock") from exc
    try:
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "r+b", closefd=True) as lock_handle:
            try:
                fcntl.flock(
                    lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise SessionDatabaseMigrationInProgress(
                    "MIGRATION_IN_PROGRESS: another primary T1000 process is "
                    "publishing the managed session database"
                ) from exc
            if target.exists():
                _check_sqlite_database(target)
                _harden_database_permissions(target)
                digest = _sha256_file(target)
                return SessionDatabaseMigration(
                    "destination_exists",
                    source,
                    target,
                    destination_sha256=digest,
                )
            _check_sqlite_database(source)
            source_digest = _sha256_file(source)
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.migration-",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(temporary_fd, 0o600)
                os.close(temporary_fd)
                temporary_fd = -1
                source_connection = sqlite3.connect(
                    f"{source.as_uri()}?mode=ro",
                    uri=True,
                    timeout=5.0,
                )
                try:
                    source_generation = int(
                        source_connection.execute("PRAGMA data_version").fetchone()[0]
                    )
                    target_connection = sqlite3.connect(str(temporary), timeout=5.0)
                    try:
                        source_connection.backup(target_connection)
                        target_connection.commit()
                    finally:
                        target_connection.close()
                    if int(
                        source_connection.execute("PRAGMA data_version").fetchone()[0]
                    ) != source_generation:
                        raise SessionDatabaseMigrationError(
                            "legacy session database changed during migration; retry "
                            "after the old T1000 writer exits"
                        )
                finally:
                    source_connection.close()
                _check_sqlite_database(temporary)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    _check_sqlite_database(target)
                    _harden_database_permissions(target)
                    digest = _sha256_file(target)
                    return SessionDatabaseMigration(
                        "destination_exists",
                        source,
                        target,
                        source_sha256=source_digest,
                        destination_sha256=digest,
                    )
                os.unlink(temporary)
                directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                target_digest = _sha256_file(target)
                return SessionDatabaseMigration(
                    "migrated",
                    source,
                    target,
                    source_sha256=source_digest,
                    destination_sha256=target_digest,
                )
            except (OSError, sqlite3.Error) as exc:
                raise SessionDatabaseMigrationError(
                    f"legacy session database migration failed: {exc}"
                ) from exc
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise SessionDatabaseMigrationError("cannot lock managed session database") from exc


class SessionStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        auth_root: str | Path | None = None,
        account_source: str | Path | None = None,
        provision_auth_scopes: bool | None = None,
    ):
        self.path = (
            Path(path).expanduser()
            if path is not None
            else default_db_path()
        )
        self._managed_state = _is_managed_state_path(self.path)
        self.auth_root = (
            Path(auth_root).expanduser()
            if auth_root is not None
            else (
                default_auth_root()
                if path is None
                else self.path.parent / "terminal-auth"
            )
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
            if self._managed_state:
                managed_root = managed_state_dir()
                managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                managed_root.chmod(0o700)
                self.path.parent.chmod(0o700)
                _harden_database_permissions(self.path)
        if self.provision_auth_scopes:
            self.auth_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.auth_root.chmod(0o700)
        # Keep the GUI responsive if a second TermDeck/CLI process briefly
        # holds the registry, and avoid a full fsync for every tab-state update.
        self.conn = sqlite3.connect(str(self.path), timeout=1.0)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()
        if self._managed_state:
            _harden_database_permissions(self.path)

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
                recovery_last_error TEXT,
                recovery_intent TEXT NOT NULL DEFAULT 'legacy'
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
        self._ensure_column(
            "recovery_intent",
            "TEXT NOT NULL DEFAULT 'legacy'",
        )
        self._clear_managed_server_local_pane_targets()
        self._backfill_recovery_intents()
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

    def _backfill_recovery_intents(self) -> int:
        """Convert status-only rows to an explicit safe recovery intent.

        The migration only fills legacy/unknown values.  An explicit active or
        inactive decision made by a newer writer is never overwritten while a
        second process opens the database.
        """

        rows = self.conn.execute(
            "SELECT id, status, recovery_intent FROM sessions"
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            current = normalise_recovery_intent(row["recovery_intent"])
            raw = str(row["recovery_intent"] or "").strip().casefold()
            if raw in {
                RECOVERY_INTENT_ACTIVE,
                RECOVERY_INTENT_INACTIVE,
            }:
                continue
            inferred = recovery_intent_for_status(row["status"])
            if current == inferred and raw == RECOVERY_INTENT_LEGACY and inferred == RECOVERY_INTENT_LEGACY:
                # Ambiguous rows (notably start-error) intentionally remain
                # legacy so startup cannot infer permission from status alone.
                continue
            updates.append((inferred, str(row["id"])))
        if not updates:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            UPDATE sessions
            SET recovery_intent = ?
            WHERE id = ?
              AND lower(trim(COALESCE(recovery_intent, ''))) NOT IN (?, ?)
            """,
            [
                (intent, session_id, RECOVERY_INTENT_ACTIVE, RECOVERY_INTENT_INACTIVE)
                for intent, session_id in updates
            ],
        )
        return max(0, int(self.conn.total_changes - before))

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

    @staticmethod
    def _record_with_recovery_intent(
        record: SessionRecord,
        *,
        existing: SessionRecord | None = None,
        preserve_legacy: bool = False,
    ) -> SessionRecord:
        raw = normalise_recovery_intent(getattr(record, "recovery_intent", None))
        if raw == RECOVERY_INTENT_LEGACY and not preserve_legacy:
            if existing is not None:
                existing_intent = normalise_recovery_intent(existing.recovery_intent)
                if existing_intent in {
                    RECOVERY_INTENT_ACTIVE,
                    RECOVERY_INTENT_INACTIVE,
                }:
                    raw = existing_intent
                else:
                    raw = recovery_intent_for_status(record.status)
            else:
                raw = recovery_intent_for_status(record.status)
        if bool(getattr(record, "external", False)) and raw == RECOVERY_INTENT_LEGACY:
            raw = RECOVERY_INTENT_INACTIVE
        return replace(record, recovery_intent=raw)

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
            recovery_intent=RECOVERY_INTENT_INACTIVE,
        )
        self.upsert(record)
        return record

    def upsert(self, record: SessionRecord, *, preserve_legacy_intent: bool = False) -> None:
        existing = self.get(record.id)
        record = self._record_with_recovery_intent(
            record,
            existing=existing,
            preserve_legacy=preserve_legacy_intent,
        )
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
                recovery_last_attempt_at, recovery_last_error, recovery_intent
            )
            VALUES (
                :id, :name, :tag, :command, :cwd, :tmux_name, :created_at, :last_opened_at,
                :status, :last_pid, :window_id, :window_title, :tab_index, :visible, :pane_target, :external, :pinned,
                :recovery_command, :recovery_cwd, :auth_scope_id, :codex_home, :selected_account,
                :pending_account, :auth_restart_required, :auth_changed_at,
                :recovery_recipe, :recovery_recipe_sha256, :restart_policy, :recovery_attempts,
                :recovery_last_attempt_at, :recovery_last_error, :recovery_intent
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
                recovery_last_error = excluded.recovery_last_error,
                recovery_intent = excluded.recovery_intent
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
        if "recovery_intent" in changes:
            data["recovery_intent"] = normalise_recovery_intent(
                changes["recovery_intent"]
            )
        elif "status" in changes:
            status = str(data.get("status", "")).strip().casefold()
            if status == "start-error":
                # Preserve an already-proven active task across a failed
                # restart; an initial launch remains ambiguous and fail-closed.
                data["recovery_intent"] = (
                    RECOVERY_INTENT_ACTIVE
                    if current.recovery_intent == RECOVERY_INTENT_ACTIVE
                    else RECOVERY_INTENT_LEGACY
                )
            else:
                data["recovery_intent"] = recovery_intent_for_status(status)
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
        preserve_legacy_intent = (
            "recovery_intent" in changes
            or str(data.get("status", "")).strip().casefold() == "start-error"
        )
        updated = self._record_with_recovery_recipe(SessionRecord(**data))
        updated = self._record_with_recovery_intent(
            updated,
            existing=current,
            preserve_legacy=preserve_legacy_intent,
        )
        self.upsert(
            updated,
            preserve_legacy_intent=preserve_legacy_intent,
        )
        return self.get(session_id) or updated

    def set_recovery_intent(self, session_id: str, intent: object) -> SessionRecord:
        """Persist an explicit lifecycle intent for one session."""

        normalized = normalise_recovery_intent(intent)
        if normalized not in RECOVERY_INTENTS:
            raise ValueError(f"unsupported recovery intent: {intent!r}")
        return self.update(
            session_id,
            recovery_intent=normalized,
        )

    def mark_recovery_active(self, session_id: str) -> SessionRecord:
        return self.set_recovery_intent(session_id, RECOVERY_INTENT_ACTIVE)

    def mark_recovery_inactive(self, session_id: str) -> SessionRecord:
        return self.set_recovery_intent(session_id, RECOVERY_INTENT_INACTIVE)

    def update_runtime_states(
        self,
        updates: Iterable[tuple[str, str, bool, int | None]],
    ) -> int:
        """Apply reconciliation state changes without reordering browser history."""

        rows = list(updates)
        if not rows:
            return 0
        changed = 0
        active_statuses = _ACTIVE_RECOVERY_STATUSES | {"start-error"}
        for session_id, status, visible, last_pid in rows:
            if str(status).strip().casefold() in active_statuses:
                cursor = self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, visible = ?, last_pid = ?,
                        recovery_intent = CASE
                            WHEN recovery_intent = ? THEN recovery_intent
                            ELSE ?
                        END
                    WHERE id = ?
                      AND NOT (
                          recovery_intent = ?
                          AND status IN ('stopped', 'archived')
                      )
                    """,
                    (
                        status,
                        1 if visible else 0,
                        last_pid,
                        RECOVERY_INTENT_INACTIVE,
                        RECOVERY_INTENT_ACTIVE,
                        session_id,
                        RECOVERY_INTENT_INACTIVE,
                    ),
                )
            else:
                # A missing/stopped observation is not an explicit user stop.
                # Never clear active intent here; the next startup can still
                # recover the task from its durable recipe.
                cursor = self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, visible = ?, last_pid = ?
                    WHERE id = ?
                    """,
                    (status, 1 if visible else 0, last_pid, session_id),
                )
            changed += max(0, int(cursor.rowcount))
        self.conn.commit()
        return changed

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
            recovery_intent=normalise_recovery_intent(row["recovery_intent"]),
        )
