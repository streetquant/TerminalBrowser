from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_config_scope import prepare_shared_codex_configuration
from .models import SessionRecord


_SQLX_MIGRATION_COLUMNS = (
    "version",
    "description",
    "installed_on",
    "success",
    "checksum",
    "execution_time",
)
_SUPPORTED_STATE_MIGRATION_VERSION = 46
_PINNED_THREAD_SECTION_ID = "01984de2-8f74-7c91-a3b2-5c5e937cf318"
_REQUIRED_STATE_TABLES = frozenset(
    {
        "_sqlx_migrations",
        "backfill_state",
        "external_agent_config_imports",
        "remote_control_enrollments",
        "thread_dynamic_tools",
        "thread_sections",
        "thread_spawn_edges",
        "threads",
    }
)
_ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REGISTRY_SELECTION_KEYS = (
    "active_account_key",
    "previous_active_account_key",
    "active_account_activated_at_ms",
    "active_email",
    "activeEmail",
)
_CODEX_AUTH_LOCK_RUNNER = ".termdeck-codex-auth-lock-runner"


class CodexAuthError(RuntimeError):
    """Raised when a private Codex account operation cannot be completed."""


@dataclass(frozen=True)
class CodexAccount:
    account_key: str
    email: str = ""
    alias: str = ""
    account_name: str = ""
    active: bool = False

    @property
    def label(self) -> str:
        return self.alias or self.account_name or self.email or self.account_key

    @property
    def switch_selector(self) -> str:
        # Stable codex-auth releases reliably accept aliases and emails. The
        # opaque account key remains a final fallback for newer releases.
        return self.alias or self.email or self.account_key

    def to_dict(self) -> dict[str, object]:
        return {
            "account_key": self.account_key,
            "email": self.email,
            "alias": self.alias,
            "account_name": self.account_name,
            "label": self.label,
            "active": self.active,
        }


@dataclass(frozen=True)
class CodexAuthState:
    codex_home: str
    accounts: tuple[CodexAccount, ...]
    selected_account: str | None
    selected_account_key: str | None
    fingerprint: str
    codex_auth_available: bool
    catalog_shared: bool = True
    canonical_binding_ready: bool = True
    canonical_account_key: str | None = None
    canonical_account: str | None = None
    marker_account_key: str | None = None
    credential_status: str = "unknown"
    canonical_credential_status: str = "unknown"
    selection_conflict: bool = False
    effective_fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "codex_home": self.codex_home,
            "selected_account": self.selected_account,
            "selected_account_key": self.selected_account_key,
            "fingerprint": self.fingerprint,
            "codex_auth_available": self.codex_auth_available,
            "catalog_shared": self.catalog_shared,
            "canonical_binding_ready": self.canonical_binding_ready,
            "canonical_account_key": self.canonical_account_key,
            "canonical_account": self.canonical_account,
            "marker_account_key": self.marker_account_key,
            "effective_account_key": self.selected_account_key,
            "effective_account": self.selected_account,
            "credential_status": self.credential_status,
            "canonical_credential_status": self.canonical_credential_status,
            "selection_conflict": self.selection_conflict,
            "accounts": [account.to_dict() for account in self.accounts],
        }


@dataclass(frozen=True)
class CodexSwitchResult:
    ok: bool
    state: CodexAuthState
    error: str = ""


def default_account_source() -> Path:
    configured = os.environ.get("TERMDECK_CODEX_ACCOUNT_SOURCE", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def new_auth_scope_id() -> str:
    return f"auth-{uuid.uuid4().hex}"


def _write_private_text(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise CodexAuthError(f"private Codex file cannot be inspected safely: {path}") from exc
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_nlink != 1
    ):
        raise CodexAuthError(f"private Codex file is unsafe: {path}")
    current = None
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    if current != content:
        temporary = path.with_name(
            f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o700 if executable else 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    path.chmod(0o700 if executable else 0o600)


def _planned_shared_session_roots(home: Path, source: Path) -> list[tuple[Path, Path]]:
    planned: list[tuple[Path, Path]] = []
    for name in ("sessions", "archived_sessions"):
        source_root = source / name
        if not source_root.is_dir():
            continue
        try:
            source_root = source_root.resolve(strict=True)
        except OSError:
            continue

        private_root = home / name
        if private_root.resolve(strict=False) == source_root:
            # The canonical source itself is not an auth-isolation target.
            # Never replace a source session root with a self-referential link.
            continue
        if private_root.is_symlink():
            try:
                target = private_root.resolve(strict=False)
            except OSError as exc:
                raise CodexAuthError(
                    f"private Codex {name} link cannot be resolved: {private_root}"
                ) from exc
            if target != source_root:
                raise CodexAuthError(
                    f"private Codex {name} link points outside the canonical session store: {private_root}"
                )
            planned.append((private_root, source_root))
            continue

        if private_root.exists():
            if not private_root.is_dir():
                raise CodexAuthError(
                    f"private Codex {name} path is not a directory: {private_root}"
                )
            try:
                next(private_root.iterdir())
            except StopIteration:
                pass
            except OSError as exc:
                raise CodexAuthError(
                    f"private Codex {name} directory cannot be inspected: {private_root}"
                ) from exc
            else:
                # Preserve non-empty legacy rollout trees exactly. They may
                # belong to a still-running Codex process.
                continue
        planned.append((private_root, source_root))
    return planned


def _share_codex_session_roots(home: Path, source: Path) -> None:
    """Share Codex conversation storage while keeping credentials private.

    ``CODEX_HOME`` controls both authentication and rollout discovery. A
    terminal-private home therefore hides pre-existing conversations unless
    its session roots point back at the canonical Codex home. New or empty
    private roots are linked to the canonical store. Non-empty legacy roots
    are deliberately preserved in place: moving a live rollout is riskier
    than leaving that one legacy scope local until it can be migrated safely.
    """

    for private_root, source_root in _planned_shared_session_roots(home, source):
        if private_root.is_symlink():
            continue
        if private_root.exists():
            try:
                private_root.rmdir()
            except OSError as exc:
                raise CodexAuthError(
                    f"private Codex session root changed before it could be shared: {private_root}"
                ) from exc

        try:
            private_root.symlink_to(source_root, target_is_directory=True)
        except FileExistsError:
            try:
                matches = private_root.is_symlink() and private_root.resolve(strict=False) == source_root
            except OSError:
                matches = False
            if not matches:
                raise CodexAuthError(
                    f"private Codex session root changed before it could be shared: {private_root}"
                )


def canonical_codex_state_db_candidates(
    private_db: str | Path,
    *,
    account_source: str | Path | None = None,
) -> list[Path]:
    """Return bounded canonical state candidates for schema and row reads.

    An explicit ``TERMDECK_CODEX_STATE_SOURCE`` is authoritative. Tests and
    alternate account catalogues stay isolated by considering only their own
    state database. The normal desktop path also considers the contained-Codex
    runtime homes because that database may have newer migrations than the
    legacy account catalogue under ``~/.codex``.
    """

    configured = os.environ.get("TERMDECK_CODEX_STATE_SOURCE", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    elif account_source is not None:
        candidates.append(Path(account_source).expanduser() / "state_5.sqlite")
    else:
        private_home = Path(private_db).expanduser().resolve(strict=False).parent
        for name in ("sessions", "archived_sessions"):
            shared_root = private_home / name
            if not shared_root.is_symlink():
                continue
            try:
                resolved_root = shared_root.resolve(strict=True)
            except OSError:
                continue
            candidates.append(resolved_root.parent / "state_5.sqlite")
        candidates.append(default_account_source() / "state_5.sqlite")
        runtime_root = Path("/var/lib/codex-runtime")
        preferred_runtime = runtime_root / Path.home().name / "state_5.sqlite"
        candidates.append(preferred_runtime)

    private_resolved = Path(private_db).expanduser().resolve(strict=False)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=False)
        except OSError:
            continue
        key = str(resolved)
        if resolved == private_resolved or key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def codex_state_migration_identity(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, bytes], ...] | None:
    """Return a strict SQLx migration identity suitable for sparse cloning.

    Codex validates known migration checksums and runs any known migration that
    is absent from the ledger.  A truthy ``success`` value or merely selecting
    the greatest version is therefore not enough: accepting a gap can make the
    native migrator replay DDL over an already-final schema.
    """

    try:
        columns = tuple(
            str(row[1])
            for row in connection.execute('PRAGMA table_info("_sqlx_migrations")')
        )
        if columns != _SQLX_MIGRATION_COLUMNS:
            return None
        rows = connection.execute(
            """
            SELECT version, typeof(version), success, typeof(success),
                   checksum, typeof(checksum)
            FROM _sqlx_migrations
            ORDER BY version
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    identity: list[tuple[int, bytes]] = []
    for row in rows:
        version, version_type, success, success_type, checksum, checksum_type = row
        if (
            type(version) is not int
            or version_type != "integer"
            or version <= 0
            or type(success) is not int
            or success_type != "integer"
            or success != 1
            or type(checksum) is not bytes
            or checksum_type != "blob"
            or len(checksum) != 48
        ):
            return None
        identity.append((version, checksum))
    if not identity:
        return None
    versions = [version for version, _checksum in identity]
    if versions != list(range(1, versions[-1] + 1)):
        return None
    return tuple(identity)


def codex_state_backfill_complete(connection: sqlite3.Connection) -> bool:
    """Match native Codex's exact, case-sensitive complete-state gate."""

    try:
        rows = connection.execute(
            """
            SELECT status, last_success_at, updated_at
            FROM backfill_state
            WHERE id=1
            """
        ).fetchall()
    except sqlite3.Error:
        return False
    if len(rows) != 1:
        return False
    status, last_success_at, updated_at = rows[0]
    return (
        status == "complete"
        and type(last_success_at) is int
        and type(updated_at) is int
        and 0 <= last_success_at <= 253402300799
        and 0 <= updated_at <= 253402300799
    )


def _validate_private_state_file(
    path: Path,
    *,
    canonical_candidates: list[Path],
    expected_identity: tuple[tuple[int, bytes], ...] | None = None,
) -> tuple[tuple[int, bytes], ...]:
    if path.is_symlink() or not path.is_file():
        raise CodexAuthError(f"private Codex state path is not a regular file: {path}")
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise CodexAuthError(f"private Codex state path cannot be inspected: {path}") from exc
    if stat_result.st_nlink != 1:
        raise CodexAuthError(
            f"private Codex state database must not be hard-linked: {path}"
        )
    for candidate in canonical_candidates:
        try:
            aliases_canonical = candidate.is_file() and os.path.samefile(path, candidate)
        except OSError:
            aliases_canonical = False
        if aliases_canonical:
            raise CodexAuthError(
                f"private Codex state database aliases canonical state: {path}"
            )
    try:
        connection = _readonly_sqlite(path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise CodexAuthError(
            f"private Codex state database cannot be opened safely: {path}"
        ) from exc
    try:
        connection.execute("BEGIN")
        identity = codex_state_migration_identity(connection)
        complete = codex_state_backfill_complete(connection)
    except sqlite3.Error as exc:
        raise CodexAuthError(
            f"private Codex state database cannot be read consistently: {path}"
        ) from exc
    finally:
        connection.close()
    if identity is None:
        raise CodexAuthError(
            f"private Codex state migration ledger is incomplete or incompatible: {path}"
        )
    if not complete:
        raise CodexAuthError(
            f"private Codex state backfill is not complete: {path}"
        )
    if expected_identity is not None and identity != expected_identity:
        raise CodexAuthError(
            f"private Codex state database changed during initialization: {path}"
        )
    return identity


def _state_template_version(path: Path) -> int | None:
    """Return a complete template's newest successful migration version."""

    if not path.is_file():
        return None
    try:
        connection = _readonly_sqlite(path)
    except (OSError, sqlite3.Error, ValueError):
        return None
    try:
        connection.execute("BEGIN")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not _REQUIRED_STATE_TABLES.issubset(tables):
            return None
        identity = codex_state_migration_identity(connection)
        if (
            identity is None
            or identity[-1][0] != _SUPPORTED_STATE_MIGRATION_VERSION
            or not codex_state_backfill_complete(connection)
        ):
            return None
        return identity[-1][0]
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        connection.close()


def _complete_state_template(
    private_db: Path,
    *,
    account_source: str | Path | None,
) -> Path | None:
    ranked: list[tuple[int, int, Path]] = []
    for index, candidate in enumerate(
        canonical_codex_state_db_candidates(
            private_db,
            account_source=account_source,
        )
    ):
        version = _state_template_version(candidate)
        if version is not None:
            # Prefer the newest compatible migration ledger. Preserve candidate
            # order as the tie-breaker so explicit and user-specific paths win.
            ranked.append((version, -index, candidate))
    return max(ranked)[2] if ranked else None


def _shared_session_roots(home: Path) -> tuple[list[Path], bool]:
    roots: list[Path] = []
    nonempty = False
    for name in ("sessions", "archived_sessions"):
        candidate = home / name
        if not candidate.is_symlink():
            continue
        try:
            root = candidate.resolve(strict=True)
            if not root.is_dir():
                raise OSError("target is not a directory")
            roots.append(root)
            if not nonempty:
                try:
                    next(root.iterdir())
                except StopIteration:
                    pass
                else:
                    nonempty = True
        except OSError as exc:
            raise CodexAuthError(
                f"shared Codex session root cannot be inspected: {candidate}"
            ) from exc
    return roots, nonempty


def _clone_sparse_complete_state(source_path: Path, destination: Path) -> Path:
    """Publish an empty, migration-complete state DB without copying history."""

    temporary = destination.with_name(
        f".{destination.name}.termdeck-init-{os.getpid()}-{uuid.uuid4().hex}"
    )
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = _readonly_sqlite(source_path)
        source.execute("BEGIN")
        migration_columns = [
            str(row[1])
            for row in source.execute('PRAGMA table_info("_sqlx_migrations")')
        ]
        backfill_columns = [
            str(row[1])
            for row in source.execute('PRAGMA table_info("backfill_state")')
        ]
        required_backfill = {
            "id",
            "status",
            "last_watermark",
            "last_success_at",
            "updated_at",
        }
        if (
            tuple(migration_columns) != _SQLX_MIGRATION_COLUMNS
            or not required_backfill.issubset(backfill_columns)
        ):
            raise CodexAuthError(
                f"canonical Codex state schema is incomplete: {source_path}"
            )
        quoted_migrations = ",".join(f'"{column}"' for column in migration_columns)
        quoted_backfill = ",".join(f'"{column}"' for column in backfill_columns)
        migrations = source.execute(
            f"SELECT {quoted_migrations} FROM _sqlx_migrations ORDER BY version"
        ).fetchall()
        backfill = source.execute(
            f"SELECT {quoted_backfill} FROM backfill_state WHERE id=1"
        ).fetchone()
        source_identity = codex_state_migration_identity(source)
        if (
            source_identity is None
            or source_identity[-1][0] != _SUPPORTED_STATE_MIGRATION_VERSION
            or backfill is None
            or not codex_state_backfill_complete(source)
        ):
            raise CodexAuthError(
                f"canonical Codex state database has an active or incomplete backfill: {source_path}"
            )
        section_columns = [
            str(row[1])
            for row in source.execute('PRAGMA table_info("thread_sections")')
        ]
        sections: list[sqlite3.Row] = []
        if section_columns:
            if not {"id", "name"}.issubset(section_columns):
                raise CodexAuthError(
                    f"canonical Codex thread section schema is incomplete: {source_path}"
                )
            quoted_sections = ",".join(f'"{column}"' for column in section_columns)
            sections = source.execute(
                f"SELECT {quoted_sections} FROM thread_sections WHERE id=?",
                (_PINNED_THREAD_SECTION_ID,),
            ).fetchall()
            if len(sections) != 1 or sections[0]["name"] != "Pinned":
                raise CodexAuthError(
                    f"canonical Codex state database is missing its pinned section: {source_path}"
                )
        schema_objects = source.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'view', 'index', 'trigger')
            ORDER BY CASE type
                WHEN 'table' THEN 0
                WHEN 'view' THEN 1
                WHEN 'index' THEN 2
                WHEN 'trigger' THEN 3
                ELSE 4
            END, name
            """
        ).fetchall()
        if not schema_objects:
            raise CodexAuthError(
                f"canonical Codex state database has no cloneable schema: {source_path}"
            )
        user_version = int(source.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(source.execute("PRAGMA application_id").fetchone()[0])

        target = sqlite3.connect(temporary, timeout=5.0)
        # Codex opens new state databases with incremental auto-vacuum. This
        # must be selected before any table is created; changing a populated
        # database later requires a full VACUUM and is not safe at startup.
        target.execute("PRAGMA auto_vacuum=INCREMENTAL")
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("BEGIN IMMEDIATE")
        for row in schema_objects:
            target.execute(str(row["sql"]))
        migration_placeholders = ",".join("?" for _ in migration_columns)
        target.executemany(
            f"INSERT INTO _sqlx_migrations ({quoted_migrations}) VALUES ({migration_placeholders})",
            ([row[column] for column in migration_columns] for row in migrations),
        )
        backfill_placeholders = ",".join("?" for _ in backfill_columns)
        completed_at = int(time.time())
        private_backfill = {
            column: backfill[column]
            for column in backfill_columns
        }
        private_backfill.update(
            {
                "id": 1,
                "status": "complete",
                "last_watermark": None,
                "last_success_at": completed_at,
                "updated_at": completed_at,
            }
        )
        target.execute(
            f"INSERT INTO backfill_state ({quoted_backfill}) VALUES ({backfill_placeholders})",
            [private_backfill[column] for column in backfill_columns],
        )
        if section_columns and sections:
            section_placeholders = ",".join("?" for _ in section_columns)
            target.executemany(
                f"INSERT INTO thread_sections ({quoted_sections}) VALUES ({section_placeholders})",
                ([row[column] for column in section_columns] for row in sections),
            )
        target.execute(f"PRAGMA user_version={user_version}")
        target.execute(f"PRAGMA application_id={application_id}")
        if codex_state_migration_identity(target) != source_identity:
            raise CodexAuthError(
                f"sparse private Codex migration ledger did not clone exactly from {source_path}"
            )
        if not codex_state_backfill_complete(target):
            raise CodexAuthError(
                f"sparse private Codex backfill marker is invalid for {source_path}"
            )
        quick_check = target.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise CodexAuthError(
                f"sparse private Codex state failed SQLite integrity checks for {source_path}"
            )
        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CodexAuthError(
                f"sparse private Codex state failed foreign-key checks for {source_path}"
            )
        target.commit()
        target.close()
        target = None
        source.close()
        source = None

        temporary.chmod(0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            # A concurrent initializer publishes with a hard link and removes
            # its temporary name immediately afterward. Give that bounded
            # publication window time to settle before applying the permanent
            # hard-link rejection used for all private databases.
            for _attempt in range(20):
                try:
                    if destination.stat().st_nlink == 1:
                        break
                except OSError:
                    break
                time.sleep(0.01)
            _validate_private_state_file(
                destination,
                canonical_candidates=[source_path],
                expected_identity=source_identity,
            )
        else:
            temporary.unlink()
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return destination
    except CodexAuthError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CodexAuthError(
            f"could not initialize sparse private Codex state from {source_path}: {exc}"
        ) from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_private_codex_state(
    codex_home: str | Path,
    *,
    account_source: str | Path | None = None,
    session_source: str | Path | None = None,
    require_complete: bool = False,
) -> Path | None:
    """Initialize cold shared-history scopes without native full backfill.

    Existing databases are never opened for write here: they may belong to a
    running Codex process. A cold scope receives only the canonical schema,
    SQLx migration ledger, and an already-complete backfill marker. Conversation
    rows remain empty until Codex creates one or exact recovery hydrates one.
    """

    home = Path(codex_home).expanduser().resolve(strict=False)
    destination = home / "state_5.sqlite"
    canonical_candidates = canonical_codex_state_db_candidates(
        destination,
        account_source=account_source,
    )
    if destination.is_symlink():
        raise CodexAuthError(
            f"private Codex state database cannot be shared by symlink: {destination}"
        )
    if destination.exists():
        if not destination.is_file():
            raise CodexAuthError(
                f"private Codex state path is not a regular file: {destination}"
            )
        # Preparation may run while this private database has a live writer;
        # inspect only filesystem identity then and never falsify its native
        # backfill state. A stopped session launch opts into the strict marker
        # and ledger gate below.
        if destination.stat().st_nlink != 1:
            raise CodexAuthError(
                f"private Codex state database must not be hard-linked: {destination}"
            )
        for candidate in canonical_candidates:
            try:
                if candidate.is_file() and os.path.samefile(destination, candidate):
                    raise CodexAuthError(
                        f"private Codex state database aliases canonical state: {destination}"
                    )
            except OSError:
                continue
        if require_complete:
            _validate_private_state_file(
                destination,
                canonical_candidates=canonical_candidates,
            )
        return destination

    if session_source is None:
        shared_roots, shared_history_present = _shared_session_roots(home)
    else:
        source = Path(session_source).expanduser().resolve(strict=False)
        planned = _planned_shared_session_roots(home, source)
        shared_roots = [source_root for _private_root, source_root in planned]
        shared_history_present = False
        for source_root in shared_roots:
            try:
                next(source_root.iterdir())
            except StopIteration:
                continue
            except OSError as exc:
                raise CodexAuthError(
                    f"canonical Codex session root cannot be inspected: {source_root}"
                ) from exc
            else:
                shared_history_present = True
                break
    if not shared_roots:
        return None
    template = _complete_state_template(
        destination,
        account_source=account_source,
    )
    if template is None:
        if shared_history_present:
            raise CodexAuthError(
                "shared Codex history requires a complete canonical Codex state database "
                "with the supported schema; "
                "refusing an unbounded native backfill"
            )
        return None
    return _clone_sparse_complete_state(template, destination)


def _clear_registry_active_account(registry_path: Path) -> None:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return
    if not isinstance(payload, dict):
        return
    changed = False
    for key in _REGISTRY_SELECTION_KEYS:
        if payload.get(key) is not None:
            payload[key] = None
            changed = True
    if changed:
        _write_private_text(
            registry_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )


def _is_termdeck_wrapper(candidate: Path) -> bool:
    try:
        if not candidate.is_file():
            return False
        with candidate.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return (
        b".termdeck-codex-auth-lock-runner" in head
        or (
            b".termdeck-compat-home" in head
            and b"export CODEX_HOME=" in head
        )
    )


def _sanitized_host_path(codex_home: Path) -> str:
    private_bin = (codex_home / "bin").resolve(strict=False)
    clean: list[str] = []
    for raw_entry in os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        entry_path = Path(entry).expanduser().resolve(strict=False)
        if entry_path == private_bin:
            continue
        if _is_termdeck_wrapper(entry_path / "codex-auth"):
            continue
        clean.append(str(entry_path))
    return os.pathsep.join(dict.fromkeys(clean)) or "/usr/local/bin:/usr/bin:/bin"


def _real_codex_auth(codex_home: Path) -> Path | None:
    configured = os.environ.get("TERMDECK_CODEX_AUTH_BINARY", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve(strict=False)
        if candidate.is_file() and os.access(candidate, os.X_OK) and not _is_termdeck_wrapper(candidate):
            return candidate

    search_dirs = [Path(entry) for entry in _sanitized_host_path(codex_home).split(os.pathsep)]
    # Desktop launchers commonly inherit a deliberately small systemd user
    # PATH (for example /usr/local/bin:/usr/bin) even though interactive
    # shells add user package-manager bins. codex-auth is commonly installed
    # by npm under ~/.npm-global/bin, so relying on the GUI's PATH makes the
    # account selector look unavailable while the command works in a shell.
    user_home = Path.home().expanduser()
    search_dirs.extend(
        [
            user_home / ".local" / "bin",
            user_home / ".npm-global" / "bin",
            user_home / ".bun" / "bin",
            user_home / ".local" / "share" / "pnpm",
        ]
    )
    seen: set[Path] = set()
    for entry in search_dirs:
        entry = entry.expanduser().resolve(strict=False)
        if entry in seen:
            continue
        seen.add(entry)
        candidate = entry / "codex-auth"
        if candidate.is_file() and os.access(candidate, os.X_OK) and not _is_termdeck_wrapper(candidate):
            return candidate.resolve(strict=False)
    return None


def _legacy_codex_lifetime_gate_root() -> Path:
    """Return the trusted legacy gate root captured by TermDeck itself."""

    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    if configured:
        cache_home = Path(configured)
        if not cache_home.is_absolute():
            raise CodexAuthError(
                "XDG_CACHE_HOME must be absolute for the legacy Codex lifetime gate"
            )
    else:
        try:
            cache_home = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".cache"
        except (KeyError, OSError) as exc:
            raise CodexAuthError(
                "could not resolve the user home for the legacy Codex lifetime gate"
            ) from exc
    if not cache_home.is_absolute():
        raise CodexAuthError(
            "user cache home must be absolute for the legacy Codex lifetime gate"
        )
    return Path(os.path.abspath(os.fspath(cache_home))) / "codex-contained-startup"


def _compat_lock_runner(codex_home: Path) -> str:
    """Run native codex-auth while exclusively owning this auth scope."""

    # This helper is a separate executable instead of an inline ``python -c``
    # snippet because the latter is easy to accidentally make consume the
    # picker's stdin. The helper remains the lock-owning parent while the native
    # picker runs, so a child cannot accidentally release the transaction by
    # closing inherited descriptors.
    home_literal = repr(str(codex_home))
    legacy_root_literal = repr(str(_legacy_codex_lifetime_gate_root()))
    return f'''#!/usr/bin/env python3
import ctypes
import fcntl
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path

EXIT_BUSY = 75
HOME = Path({home_literal})
LEGACY_ROOT = Path({legacy_root_literal})
SCOPE_KEY_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")


def fail(message):
    print(f"codex-auth: {{message}}", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_BUSY)


def validate_root(root, label, *, create):
    if not root.is_absolute():
        fail(f"{{label}} is not absolute")
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = root.stat(follow_symlinks=False)
    except OSError as exc:
        fail(f"{{label}} is unavailable: {{exc}}")
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"{{label}} has unsafe ownership or mode")


def acquire_lock(path, label, *, strict_mode):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        fail(f"this platform cannot open {{label}} safely")
    try:
        fd = os.open(
            path,
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            0o600,
        )
    except OSError as exc:
        fail(f"{{label}} is unavailable: {{exc}}")
    try:
        metadata = os.fstat(fd)
        path_metadata = path.stat(follow_symlinks=False)
        forbidden_mode = 0o077 if strict_mode else 0o022
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & forbidden_mode
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            fail(f"{{label}} has unsafe ownership or mode")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(f"{{label}} is owned by another Codex process")
        path_metadata = path.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            fail(f"{{label}} changed while opening")
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


if len(sys.argv) < 2:
    raise SystemExit(64)
try:
    home_metadata = HOME.stat(follow_symlinks=False)
except OSError as exc:
    fail(f"private Codex auth scope is unavailable: {{exc}}")
if (
    HOME.is_symlink()
    or not stat.S_ISDIR(home_metadata.st_mode)
    or home_metadata.st_uid != os.geteuid()
    or home_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
):
    fail("private Codex auth scope is unavailable")

scope_id = os.environ.get("TERMDECK_AUTH_SCOPE_ID", "").strip()
scope_key = SCOPE_KEY_PATTERN.sub("_", scope_id)
if not scope_key:
    fail("this terminal has no private Codex auth scope id")
canonical_root = HOME / ".termdeck-codex-lifetime"
configured_canonical_root = os.environ.get("CODEX_TERMDECK_GATE_ROOT", "").strip()
if (
    not configured_canonical_root
    or not os.path.isabs(configured_canonical_root)
    or Path(os.path.abspath(configured_canonical_root)) != canonical_root
):
    fail("this terminal has an invalid canonical Codex lifetime gate")

validate_root(LEGACY_ROOT, "legacy Codex lifetime gate", create=True)
validate_root(canonical_root, "canonical Codex lifetime gate", create=True)

# The lock order is shared with the GUI transaction coordinator. The legacy
# lease remains necessary during rolling upgrades while older launchers still
# hold the cache-root inode.
lock_specs = (
    (HOME / ".termdeck-auth-transaction.lock", "Codex auth transaction lock", True),
    (LEGACY_ROOT / f"{{scope_key}}.lock", "legacy Codex lifetime lock", False),
    (canonical_root / f"scope.{{scope_key}}.lock", "canonical Codex lifetime lock", False),
)
held_fds = []
try:
    for lock_path, label, strict_mode in lock_specs:
        held_fds.append(acquire_lock(lock_path, label, strict_mode=strict_mode))

    command = sys.argv[1]
    parent_pid = os.getpid()
    libc = ctypes.CDLL(None, use_errno=True)

    def arm_parent_death():
        # TermDeck is Linux-only. A targeted SIGKILL cannot be forwarded by
        # the supervising runner, so make the kernel kill the native mutator
        # before the runner's flock can disappear. Rechecking getppid closes
        # the race where the parent dies just before prctl takes effect.
        prctl = getattr(libc, "prctl", None)
        if prctl is None or prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os.write(2, b"codex-auth: could not arm parent-death protection\\n")
            os._exit(126)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)

    try:
        process = subprocess.Popen(
            [command, *sys.argv[2:]],
            close_fds=True,
            pass_fds=tuple(held_fds),
            preexec_fn=arm_parent_death,
        )
    except OSError as exc:
        print(f"codex-auth: could not start native codex-auth: {{exc}}", file=sys.stderr, flush=True)
        raise SystemExit(126)
    def forward_signal(signum, _frame):
        try:
            process.send_signal(signum)
        except OSError:
            pass
    for forwarded in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(forwarded, forward_signal)
    while True:
        try:
            return_code = process.wait()
            break
        except (InterruptedError, KeyboardInterrupt):
            # A terminal-generated SIGINT reaches the native foreground child
            # directly. Keep the parent alive until the child exits so Ctrl-C
            # cannot expose an unlocked command that is still mutating state.
            continue
except SystemExit:
    raise
finally:
    for held_fd in reversed(held_fds):
        try:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(held_fd)
        except OSError:
            pass
if return_code < 0:
    try:
        signal.signal(-return_code, signal.SIG_DFL)
        os.kill(os.getpid(), -return_code)
    except (OSError, ValueError):
        raise SystemExit(128 + (-return_code))
raise SystemExit(return_code)
'''


def _compat_wrapper(codex_home: Path) -> str:
    compat_home = codex_home / ".termdeck-compat-home"
    host_path = _sanitized_host_path(codex_home)
    executable = _real_codex_auth(codex_home)
    command = str(executable) if executable is not None else "codex-auth"
    lock_runner = codex_home / "bin" / _CODEX_AUTH_LOCK_RUNNER
    python = Path(sys.executable).resolve(strict=False)
    lines = [
        "#!/bin/sh",
        "set -eu",
        f"export CODEX_HOME={shlex.quote(str(codex_home))}",
        f"export CODEX_TERMDECK_GATE_ROOT={shlex.quote(str(codex_home / '.termdeck-codex-lifetime'))}",
        f"export HOME={shlex.quote(str(compat_home))}",
        f"export PATH={shlex.quote(host_path)}",
        (
            f'exec {shlex.quote(str(python))} {shlex.quote(str(lock_runner))} '
            f'{shlex.quote(command)} "$@"'
        ),
    ]
    lines.append("")
    return "\n".join(lines)


def refresh_auth_scope_wrappers(
    codex_home: str | Path,
) -> tuple[Path, Path]:
    """Atomically refresh only a private scope's generated auth executables.

    This intentionally avoids account pointers, credential catalogues, config,
    sessions, and state databases so a rolling deployment can update stopped or
    live scopes without re-running scope migration. Each executable is
    published with an atomic replace; the runner is installed first because
    both the old and new shell wrappers invoke that stable path.
    """

    requested = Path(codex_home).expanduser()
    try:
        if requested.is_symlink():
            raise CodexAuthError(
                f"private Codex auth scope must not be a symlink: {requested}"
            )
        home = requested.resolve(strict=True)
        home_metadata = home.lstat()
    except CodexAuthError:
        raise
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex auth scope cannot be inspected safely: {requested}"
        ) from exc
    if (
        not stat.S_ISDIR(home_metadata.st_mode)
        or home_metadata.st_uid != os.geteuid()
        or home_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise CodexAuthError(f"private Codex auth scope is unsafe: {home}")

    bin_dir = home / "bin"
    try:
        bin_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex auth executable directory is unavailable: {bin_dir}"
        ) from exc
    try:
        bin_metadata = bin_dir.lstat()
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex auth executable directory cannot be inspected: {bin_dir}"
        ) from exc
    if (
        bin_dir.is_symlink()
        or not stat.S_ISDIR(bin_metadata.st_mode)
        or bin_metadata.st_uid != os.geteuid()
        or bin_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise CodexAuthError(
            f"private Codex auth executable directory is unsafe: {bin_dir}"
        )

    runner = bin_dir / _CODEX_AUTH_LOCK_RUNNER
    wrapper = bin_dir / "codex-auth"
    _write_private_text(runner, _compat_lock_runner(home), executable=True)
    _write_private_text(wrapper, _compat_wrapper(home), executable=True)
    return runner, wrapper


def prepare_auth_scope(
    codex_home: str | Path,
    *,
    account_source: str | Path | None = None,
    migrate_account_catalog: bool = False,
) -> Path:
    """Create one durable Codex auth scope with shared conversation history.

    Saved credential files remain authoritative in the configured account
    source.  Native codex-auth 0.1.x scopes share that entire ``accounts``
    directory because its picker deliberately ignores symlinked directory
    entries. Registry-based layouts retain only a private registry while their
    credential entries point at the same source files. In both layouts the
    scope's ``current`` and ``auth.json`` selection stay private.

    Releases that hard-code ``~/.codex`` are isolated by running them with a
    private compatibility HOME. Codex session roots are shared with the
    canonical source home so per-tab selection never hides conversations.
    """

    home = Path(codex_home).expanduser().resolve(strict=False)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)

    source = Path(account_source).expanduser() if account_source else default_account_source()
    _share_account_catalog(
        home,
        source,
        migrate_existing=migrate_account_catalog,
    )
    prepare_shared_codex_configuration(home, source)

    private_state = ensure_private_codex_state(
        home,
        account_source=account_source,
        session_source=source,
    )
    # Never expose a large shared rollout tree to an existing private database
    # whose native backfill is still pending/running. Scope preparation may run
    # while that database has an active Codex writer. A newly initialized sparse
    # database, or an existing supported database with a natural complete marker,
    # can safely see the shared roots without triggering the native full scan.
    if (
        private_state is None
        or _state_template_version(private_state) is not None
    ):
        _share_codex_session_roots(home, source)

    compat_home = home / ".termdeck-compat-home"
    compat_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    compat_home.chmod(0o700)
    compat_codex = compat_home / ".codex"
    if compat_codex.is_symlink():
        if compat_codex.resolve(strict=False) != home:
            compat_codex.unlink()
    elif compat_codex.exists():
        raise CodexAuthError(f"private codex-auth compatibility path is not a symlink: {compat_codex}")
    if not compat_codex.exists():
        compat_codex.symlink_to(home, target_is_directory=True)

    refresh_auth_scope_wrappers(home)
    return home


def _registry_accounts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    raw_accounts = payload.get("accounts", [])
    if isinstance(raw_accounts, dict):
        accounts = []
        for key, value in raw_accounts.items():
            row = dict(value) if isinstance(value, dict) else {}
            row.setdefault("account_key", str(key))
            accounts.append(row)
    elif isinstance(raw_accounts, list):
        accounts = [row for row in raw_accounts if isinstance(row, dict)]
    else:
        accounts = []
    active_key = payload.get("active_account_key")
    if isinstance(active_key, str) and active_key.strip():
        return accounts, active_key.strip()
    active_email = payload.get("active_email") or payload.get("activeEmail")
    if isinstance(active_email, str) and active_email.strip():
        for row in accounts:
            if str(row.get("email", "")).casefold() == active_email.strip().casefold():
                key = str(row.get("account_key") or row.get("key") or active_email).strip()
                return accounts, key or None
    return accounts, None


def _source_authority(path: Path, *, kind: str, directory: bool) -> os.stat_result:
    """Validate one configured catalogue object without following its last link."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CodexAuthError(f"configured Codex {kind} cannot be inspected: {path}") from exc
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if path.is_symlink() or not expected:
        object_kind = "directory" if directory else "regular file"
        raise CodexAuthError(
            f"configured Codex {kind} is not a direct {object_kind}: {path}"
        )
    if metadata.st_uid != os.geteuid():
        raise CodexAuthError(
            f"configured Codex {kind} is not owned by the current user: {path}"
        )
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CodexAuthError(
            f"configured Codex {kind} is writable by another user: {path}"
        )
    if not directory and metadata.st_nlink != 1:
        raise CodexAuthError(
            f"configured Codex {kind} must not be hard-linked outside its source: {path}"
        )
    return metadata


def _account_name(path: Path) -> str:
    name = path.stem
    if not _ACCOUNT_NAME_PATTERN.fullmatch(name):
        raise CodexAuthError(f"configured Codex account name is unsafe: {path.name}")
    return name


def _source_account_catalog(
    source: Path,
) -> tuple[Path, dict[str, Path], dict[str, Any] | None]:
    """Resolve and validate the sole authoritative saved-account catalogue."""

    try:
        source_root = source.expanduser().resolve(strict=True)
    except FileNotFoundError:
        return source.expanduser().resolve(strict=False), {}, None
    except OSError as exc:
        raise CodexAuthError(
            f"configured Codex account source cannot be resolved: {source}"
        ) from exc
    _source_authority(source_root, kind="account source", directory=True)

    accounts = source_root / "accounts"
    try:
        accounts.lstat()
    except FileNotFoundError:
        return source_root, {}, None
    except OSError as exc:
        raise CodexAuthError(
            f"configured Codex account catalogue cannot be inspected: {accounts}"
        ) from exc
    _source_authority(accounts, kind="account catalogue", directory=True)

    try:
        entries = list(accounts.iterdir())
    except OSError as exc:
        raise CodexAuthError(
            f"configured Codex account catalogue cannot be read: {accounts}"
        ) from exc

    credentials: dict[str, Path] = {}
    registry: dict[str, Any] | None = None
    for entry in entries:
        if entry.suffix.casefold() != ".json":
            continue
        _source_authority(entry, kind="saved account file", directory=False)
        if entry.name.casefold() == "registry.json":
            try:
                parsed = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeError) as exc:
                raise CodexAuthError(
                    f"configured Codex account registry is invalid: {entry}"
                ) from exc
            if not isinstance(parsed, dict):
                raise CodexAuthError(
                    f"configured Codex account registry is invalid: {entry}"
                )
            registry = parsed
            continue
        name = _account_name(entry)
        if name.casefold() in {existing.casefold() for existing in credentials}:
            raise CodexAuthError(
                f"configured Codex account names differ only by case: {entry.name}"
            )
        credentials[name] = entry

    if registry is not None:
        rows, _active = _registry_accounts(registry)
        seen_keys: set[str] = set()
        for index, row in enumerate(rows):
            key = str(
                row.get("account_key")
                or row.get("key")
                or row.get("email")
                or f"account-{index + 1}"
            ).strip()
            if not _ACCOUNT_NAME_PATTERN.fullmatch(key):
                raise CodexAuthError(
                    f"configured Codex registry account key is unsafe: {key or '<empty>'}"
                )
            folded = key.casefold()
            if folded in seen_keys:
                raise CodexAuthError(
                    f"configured Codex registry account key is duplicated: {key}"
                )
            seen_keys.add(folded)
            if key not in credentials:
                raise CodexAuthError(
                    f"configured Codex registry account has no credential file: {key}"
                )
    return source_root, credentials, registry


def _same_link_target(link: Path, target: Path, *, directory: bool = False) -> bool:
    if not link.is_symlink():
        return False
    try:
        resolved = link.resolve(strict=True)
        target_resolved = target.resolve(strict=True)
        if resolved != target_resolved or not os.path.samefile(resolved, target_resolved):
            return False
        return resolved.is_dir() if directory else resolved.is_file()
    except OSError:
        return False


def _publish_symlink(path: Path, target: Path, *, directory: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.link-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target, target_is_directory=directory)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_completed_native_migration(home: Path, accounts: Path, source_accounts: Path) -> None:
    backup = home / ".termdeck-accounts-migration"
    if not backup.exists() and not backup.is_symlink():
        return
    if backup.is_symlink() or not backup.is_dir():
        raise CodexAuthError(
            f"private Codex account migration backup is unsafe: {backup}"
        )
    if not _same_link_target(accounts, source_accounts, directory=True):
        raise CodexAuthError(
            f"private Codex account migration is incomplete: {backup}"
        )
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex stale account snapshots could not be removed: {backup}"
        ) from exc


def _share_native_account_catalog(
    home: Path,
    source_accounts: Path,
    *,
    migrate_existing: bool,
) -> bool:
    """Make codex-auth 0.1.x enumerate the canonical directory directly."""

    accounts = home / "accounts"
    backup = home / ".termdeck-accounts-migration"
    if accounts.is_symlink():
        if not _same_link_target(accounts, source_accounts, directory=True):
            raise CodexAuthError(
                f"private Codex account catalogue link points outside the configured source: {accounts}"
            )
        return True
    if accounts.exists() and not accounts.is_dir():
        raise CodexAuthError(
            f"private Codex account catalogue is not a directory: {accounts}"
        )
    try:
        if accounts.exists() and os.path.samefile(accounts, source_accounts):
            return True
    except OSError:
        pass

    # Scope preparation is also called by selector polling while the terminal
    # may still have a Codex writer.  Replacing a legacy snapshot here would
    # silently redirect that writer to the canonical credential file.  Only an
    # explicit account transaction, after it has quiesced the terminal, may
    # perform this migration.
    if not migrate_existing and (
        accounts.exists() or backup.exists() or backup.is_symlink()
    ):
        return False

    if backup.exists() or backup.is_symlink():
        raise CodexAuthError(
            f"private Codex account migration is incomplete: {backup}"
        )
    moved_existing = False
    if accounts.exists():
        registry_path = accounts / "registry.json"
        if registry_path.exists() or registry_path.is_symlink():
            raise CodexAuthError(
                "private registry-based Codex account state cannot be migrated to a native catalogue"
            )
        try:
            os.replace(accounts, backup)
            moved_existing = True
        except OSError as exc:
            raise CodexAuthError(
                f"private Codex account catalogue could not be staged for migration: {accounts}"
            ) from exc
    try:
        _publish_symlink(accounts, source_accounts, directory=True)
        if not _same_link_target(accounts, source_accounts, directory=True):
            raise CodexAuthError(
                f"private Codex account catalogue did not bind to its configured source: {accounts}"
            )
    except Exception:
        try:
            if accounts.is_symlink():
                accounts.unlink()
            if moved_existing and backup.exists():
                os.replace(backup, accounts)
        except OSError:
            pass
        raise
    return True


def _link_registry_credential(destination: Path, source: Path) -> None:
    migration_pattern = f".{destination.name}.migration-*"
    migration_artifacts = list(destination.parent.glob(migration_pattern))
    if migration_artifacts:
        raise CodexAuthError(
            "private Codex credential migration has an unresolved stale snapshot: "
            f"{migration_artifacts[0]}"
        )
    if destination.is_symlink():
        if not _same_link_target(destination, source):
            raise CodexAuthError(
                f"private Codex account link points outside the configured source: {destination}"
            )
        return
    if destination.exists() and not destination.is_file():
        raise CodexAuthError(
            f"private Codex saved account path is not a regular file: {destination}"
        )

    backup = destination.with_name(f".{destination.name}.migration-{uuid.uuid4().hex}")
    moved_existing = False
    if destination.exists():
        try:
            os.replace(destination, backup)
            moved_existing = True
        except OSError as exc:
            raise CodexAuthError(
                f"private Codex saved account could not be staged for migration: {destination}"
            ) from exc
    try:
        _publish_symlink(destination, source)
        if not _same_link_target(destination, source):
            raise CodexAuthError(
                f"private Codex saved account did not bind to its configured source: {destination}"
            )
    except Exception:
        try:
            if destination.is_symlink():
                destination.unlink()
            if moved_existing and backup.exists():
                os.replace(backup, destination)
        except OSError:
            pass
        raise
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex stale account snapshot could not be removed: {backup}"
        ) from exc


def _registry_migration_artifacts(accounts: Path) -> list[Path]:
    """Return interrupted credential-copy staging files without reading them."""

    try:
        return sorted(accounts.glob(".*.json.migration-*"), key=lambda path: path.name)
    except OSError as exc:
        raise CodexAuthError(
            f"private Codex account catalogue cannot be inspected: {accounts}"
        ) from exc


def _private_registry_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise CodexAuthError(f"private Codex account registry is unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise CodexAuthError(f"private Codex account registry is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise CodexAuthError(f"private Codex account registry is invalid: {path}")
    return payload


def _share_registry_account_catalog(
    home: Path,
    source_accounts: Path,
    credentials: dict[str, Path],
    source_registry: dict[str, Any],
) -> None:
    """Keep selection metadata private while sharing every credential file."""

    accounts = home / "accounts"
    if accounts.is_symlink():
        raise CodexAuthError(
            "registry-based Codex accounts require a private registry; refusing a shared directory"
        )
    if accounts.exists() and not accounts.is_dir():
        raise CodexAuthError(
            f"private Codex account catalogue is not a directory: {accounts}"
        )
    accounts.mkdir(parents=True, exist_ok=True, mode=0o700)
    accounts.chmod(0o700)
    migration_artifacts = _registry_migration_artifacts(accounts)
    if migration_artifacts:
        raise CodexAuthError(
            "private Codex credential migration has an unresolved stale snapshot: "
            f"{migration_artifacts[0]}"
        )

    registry_path = accounts / "registry.json"
    private_registry = _private_registry_payload(registry_path)
    merged_registry = dict(source_registry)
    if private_registry is None:
        for key in _REGISTRY_SELECTION_KEYS:
            if key in merged_registry:
                merged_registry[key] = None
    else:
        for key in _REGISTRY_SELECTION_KEYS:
            if key in private_registry:
                merged_registry[key] = private_registry[key]

    rows, active_key = _registry_accounts(merged_registry)
    available_keys = {
        str(row.get("account_key") or row.get("key") or row.get("email") or "").strip()
        for row in rows
    }
    if active_key is not None and active_key not in available_keys:
        raise CodexAuthError(
            f"private Codex selected registry account is no longer available: {active_key}"
        )
    _write_private_text(
        registry_path,
        json.dumps(merged_registry, indent=2, sort_keys=True) + "\n",
    )

    for name, source_path in credentials.items():
        _link_registry_credential(accounts / f"{name}.json", source_path)

    for private_path in accounts.iterdir():
        if (
            private_path.name.casefold() == "registry.json"
            or private_path.suffix.casefold() != ".json"
            or private_path.stem in credentials
        ):
            continue
        if private_path.is_symlink():
            try:
                private_path.unlink()
            except OSError as exc:
                raise CodexAuthError(
                    f"private Codex stale account link could not be removed: {private_path}"
                ) from exc
        elif private_path.is_file():
            try:
                private_path.unlink()
            except OSError as exc:
                raise CodexAuthError(
                    f"private Codex stale account snapshot could not be removed: {private_path}"
                ) from exc
        else:
            raise CodexAuthError(
                f"private Codex stale account path is unsafe: {private_path}"
            )


def _native_account_names(accounts_dir: Path) -> list[str]:
    """Return codex-auth 0.1.x account names without reading credential bodies."""

    try:
        entries = list(accounts_dir.iterdir())
    except OSError:
        return []
    return sorted(
        {
            entry.stem
            for entry in entries
            if entry.is_file()
            and not entry.is_symlink()
            and entry.suffix.casefold() == ".json"
            and entry.name.casefold() != "registry.json"
            and _ACCOUNT_NAME_PATTERN.fullmatch(entry.stem)
        },
        key=str.casefold,
    )


def _native_auth_link_account(home: Path, account_names: list[str]) -> str | None:
    """Map a native auth symlink only when it stays inside ``accounts``."""

    names = set(account_names)
    auth_path = home / "auth.json"
    if auth_path.is_symlink():
        try:
            target = auth_path.resolve(strict=True)
            accounts_root = (home / "accounts").resolve(strict=False)
            relative = target.relative_to(accounts_root)
        except (OSError, ValueError):
            relative = None
        if (
            relative is not None
            and relative.parent == Path(".")
            and relative.suffix.casefold() == ".json"
            and relative.stem in names
        ):
            # auth.json is the file Codex actually consumes, so it wins if a
            # partially completed switch leaves the metadata marker stale.
            return relative.stem
    return None


def _private_current_marker_safe(home: Path) -> bool:
    current_path = home / "current"
    try:
        metadata = current_path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and metadata.st_nlink == 1
    )


def _validate_private_current_marker(home: Path) -> None:
    if not _private_current_marker_safe(home):
        raise CodexAuthError(
            f"private Codex current marker is unsafe: {home / 'current'}"
        )


def _native_current_account(home: Path, account_names: list[str]) -> str | None:
    """Read a safe native ``current`` marker without consulting auth.json."""

    current_path = home / "current"
    if not _private_current_marker_safe(home):
        return None
    try:
        current = current_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        current = ""
    return current if current in set(account_names) else None


def _native_selected_account(home: Path, account_names: list[str]) -> str | None:
    """Resolve the active account used by codex-auth 0.1.x.

    codex-auth 0.1.x writes the selected account name to ``~/.codex/current``
    and, on Unix, points ``auth.json`` at ``accounts/<name>.json``. The private
    auth symlink is authoritative because Codex consumes it directly; the
    ``current`` marker is a fallback. A link outside this terminal's private
    account directory is never accepted.
    """

    linked_account = _native_auth_link_account(home, account_names)
    if linked_account is not None:
        return linked_account
    return _native_current_account(home, account_names)


def _regular_auth_account(
    home: Path,
    credentials: dict[str, Path],
) -> str | None:
    """Map a legacy regular auth.json to one canonical account by content.

    The comparison never returns credential bytes or embeds them in an error.
    A hard-linked, writable-by-others, or ambiguous auth file is not accepted
    as selection evidence.
    """

    auth_path = home / "auth.json"
    try:
        metadata = auth_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CodexAuthError(f"private Codex auth path cannot be inspected: {auth_path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_nlink != 1
    ):
        raise CodexAuthError(f"private Codex auth path is unsafe: {auth_path}")

    try:
        auth_digest = hashlib.sha256(auth_path.read_bytes()).digest()
    except (OSError, MemoryError) as exc:
        raise CodexAuthError(f"private Codex auth path cannot be compared safely: {auth_path}") from exc
    matches: list[str] = []
    for name, credential in credentials.items():
        try:
            if metadata.st_size != credential.stat().st_size:
                continue
            if hashlib.sha256(credential.read_bytes()).digest() == auth_digest:
                matches.append(name)
        except (OSError, MemoryError) as exc:
            raise CodexAuthError(
                f"configured Codex saved account changed during selection validation: {credential}"
            ) from exc
    return matches[0] if len(matches) == 1 else None


def _canonical_default_account(
    source_root: Path,
    credentials: dict[str, Path],
    source_registry: dict[str, Any] | None,
) -> str | None:
    """Infer the canonical default from effective credentials, not a marker."""
    from .codex_credentials import observe
    observed = observe(source_root, credentials)
    return observed.key if observed.status == "current" else None


def _bind_selected_auth_pointer(home: Path, account_name: str) -> None:
    """Atomically bind the private active pointer through its shared catalogue."""

    if not _ACCOUNT_NAME_PATTERN.fullmatch(account_name):
        raise CodexAuthError(f"private Codex selected account key is unsafe: {account_name}")
    auth_path = home / "auth.json"
    if auth_path.exists() and not auth_path.is_file():
        raise CodexAuthError(f"private Codex auth path is unsafe: {auth_path}")
    relative_target = Path("accounts") / f"{account_name}.json"
    _publish_symlink(auth_path, relative_target)


def _selected_auth_pointer(
    home: Path,
    credentials: dict[str, Path],
) -> str | None:
    auth_path = home / "auth.json"
    if auth_path.is_symlink():
        return next(
            (
                name
                for name, credential in credentials.items()
                if _same_link_target(auth_path, credential)
            ),
            None,
        )
    return _regular_auth_account(home, credentials)


def _set_registry_selection(home: Path, account_name: str) -> None:
    registry_path = home / "accounts" / "registry.json"
    payload = _private_registry_payload(registry_path)
    if payload is None:
        raise CodexAuthError(f"private Codex account registry is missing: {registry_path}")
    rows, previous = _registry_accounts(payload)
    account = next(
        (
            row
            for row in rows
            if str(row.get("account_key") or row.get("key") or row.get("email") or "").strip()
            == account_name
        ),
        None,
    )
    if account is None:
        raise CodexAuthError(f"saved Codex account not found: {account_name}")
    if "previous_active_account_key" in payload:
        payload["previous_active_account_key"] = previous
    payload["active_account_key"] = account_name
    email = str(account.get("email") or "").strip() or None
    if "active_email" in payload:
        payload["active_email"] = email
    if "activeEmail" in payload:
        payload["activeEmail"] = email
    if "active_account_activated_at_ms" in payload:
        payload["active_account_activated_at_ms"] = int(time.time() * 1000)
    _write_private_text(
        registry_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _select_private_account(home: Path, account_name: str) -> None:
    """Commit one scope-local selection without mutating shared credentials."""

    if _uses_registry_switch(home):
        _set_registry_selection(home, account_name)
    else:
        _write_private_text(home / "current", f"{account_name}\n")
    # This is the commit point consumed by Codex. Metadata is written first so
    # a concurrent read still prefers the old auth link until this atomic
    # replacement succeeds.
    _bind_selected_auth_pointer(home, account_name)


def _clear_private_selection(home: Path) -> None:
    for marker in ("auth.json", "current"):
        try:
            (home / marker).unlink(missing_ok=True)
        except OSError as exc:
            raise CodexAuthError(f"private Codex selection could not be cleared: {home / marker}") from exc
    _clear_registry_active_account(home / "accounts" / "registry.json")


def _validate_selected_auth_pointer(
    home: Path,
    selected: str | None,
    credentials: dict[str, Path],
) -> None:
    auth_path = home / "auth.json"
    if auth_path.is_symlink():
        actual_name = next(
            (
                name
                for name, candidate in credentials.items()
                if _same_link_target(auth_path, candidate)
            ),
            None,
        )
        if actual_name is None:
            raise CodexAuthError(
                f"private Codex auth link points outside the configured account catalogue: {auth_path}"
            )
        if actual_name == selected:
            return
        raise CodexAuthError(
            "private Codex auth link and selected-account marker identify "
            "different saved accounts"
        )
    if auth_path.exists() and not auth_path.is_file():
        raise CodexAuthError(f"private Codex auth path is unsafe: {auth_path}")
    if selected is None:
        return
    if selected not in credentials:
        raise CodexAuthError(
            f"private Codex selected account is no longer available: {selected}"
        )


def _share_account_catalog(
    home: Path,
    source: Path,
    *,
    migrate_existing: bool,
) -> bool:
    source_root, credentials, source_registry = _source_account_catalog(source)
    source_accounts = source_root / "accounts"
    private_accounts = home / "accounts"
    new_scope_catalog = not private_accounts.exists() and not private_accounts.is_symlink()
    if not credentials and source_registry is None:
        # An absent source has no authority to validate or rewrite an existing
        # scope. Preserve it exactly; read_auth_state exposes no selectable
        # canonical accounts, so callers still cannot activate the orphaned
        # entries through this manager.
        return True
    if home == source_root:
        return True

    scope_unselected = not any(
        path.exists() or path.is_symlink()
        for path in (home / "auth.json", home / "current")
    )
    canonical_default = (
        _canonical_default_account(source_root, credentials, source_registry)
        if scope_unselected and new_scope_catalog
        else None
    )

    if source_registry is None:
        local_account_names = _native_account_names(private_accounts)
        if migrate_existing:
            _validate_private_current_marker(home)
        selected_before = _native_selected_account(
            home,
            local_account_names,
        )
        auth_path = home / "auth.json"
        regular_selection = _regular_auth_account(home, credentials)
        if migrate_existing and auth_path.is_symlink():
            link_selection = _native_auth_link_account(home, local_account_names)
            if link_selection is None or link_selection not in credentials:
                raise CodexAuthError(
                    "private Codex auth link points outside the configured account catalogue; "
                    "refusing to replace its account catalogue"
                )
            # auth.json is the effective selection for codex-auth 0.1.x. A
            # stale current marker is repaired only after the catalogue swap.
            selected_before = link_selection
        if migrate_existing and auth_path.exists() and not auth_path.is_symlink():
            if regular_selection is None:
                raise CodexAuthError(
                    "private Codex auth.json does not match exactly one configured saved account; "
                    "refusing to replace its account catalogue"
                )
            if selected_before is not None and selected_before != regular_selection:
                raise CodexAuthError(
                    "private Codex auth.json and current marker select different saved accounts; "
                    "refusing to replace its account catalogue"
                )
            selected_before = regular_selection
        shared = _share_native_account_catalog(
            home,
            source_accounts,
            migrate_existing=migrate_existing,
        )
        names = _native_account_names(private_accounts)
        selected = _native_selected_account(home, names) or selected_before
        if shared:
            _validate_selected_auth_pointer(home, selected, credentials)
            if migrate_existing and selected is not None:
                _bind_selected_auth_pointer(home, selected)
                _write_private_text(home / "current", f"{selected}\n")
            elif canonical_default is not None:
                _select_private_account(home, canonical_default)
            if migrate_existing:
                _clean_completed_native_migration(home, private_accounts, source_accounts)
        return shared

    if (
        private_accounts.exists()
        and any(private_accounts.iterdir())
        and not private_accounts.is_symlink()
        and not migrate_existing
    ):
        return False

    existing_registry = _private_registry_payload(
        private_accounts / "registry.json"
    ) or {}
    _existing_rows, selected_before = _registry_accounts(existing_registry)
    if migrate_existing and selected_before is not None:
        auth_path = home / "auth.json"
        pointer_selection = _selected_auth_pointer(home, credentials)
        if auth_path.is_symlink():
            _validate_selected_auth_pointer(home, selected_before, credentials)
        elif auth_path.exists() and pointer_selection != selected_before:
            raise CodexAuthError(
                "private Codex auth.json does not match its selected saved account; "
                "refusing to replace its account catalogue"
            )

    _share_registry_account_catalog(
        home,
        source_accounts,
        credentials,
        source_registry,
    )
    private_registry = _private_registry_payload(private_accounts / "registry.json") or {}
    _rows, selected = _registry_accounts(private_registry)
    if migrate_existing and selected is not None:
        _bind_selected_auth_pointer(home, selected)
    elif canonical_default is not None:
        _select_private_account(home, canonical_default)
        selected = canonical_default
    _validate_selected_auth_pointer(home, selected, credentials)
    return True


def _path_fingerprint(path: Path) -> bytes:
    """Fingerprint filesystem metadata without hashing token file contents."""

    try:
        link_stat = path.lstat()
    except OSError:
        return b"missing"
    marker = (
        f"{link_stat.st_dev}:{link_stat.st_mode}:{link_stat.st_size}:"
        f"{link_stat.st_mtime_ns}:{link_stat.st_ino}"
    ).encode()
    if path.is_symlink():
        try:
            target_stat = path.stat()
            target_marker = (
                f"{target_stat.st_dev}:{target_stat.st_mode}:{target_stat.st_size}:"
                f"{target_stat.st_mtime_ns}:{target_stat.st_ino}"
            ).encode()
            return (
                marker
                + b":symlink:"
                + os.readlink(path).encode("utf-8", "surrogateescape")
                + b":target:"
                + target_marker
            )
        except OSError:
            return marker + b":broken-symlink"
    return marker


def _state_fingerprint(
    home: Path,
    registry_bytes: bytes,
    account_names: list[str],
    *,
    credential_paths: dict[str, Path] | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(registry_bytes)
    digest.update(b"\0current\0")
    current_path = home / "current"
    digest.update(_path_fingerprint(current_path))
    if _private_current_marker_safe(home):
        try:
            digest.update(current_path.read_bytes())
        except OSError:
            digest.update(b"missing")
    else:
        digest.update(b"unsafe")
    digest.update(b"\0auth\0")
    digest.update(_path_fingerprint(home / "auth.json"))
    digest.update(b"\0accounts\0")
    accounts_dir = home / "accounts"
    for name in account_names:
        digest.update(name.encode("utf-8", "surrogateescape"))
        digest.update(b"=")
        credential_path = (
            credential_paths.get(name, accounts_dir / f"{name}.json")
            if credential_paths is not None
            else accounts_dir / f"{name}.json"
        )
        digest.update(_path_fingerprint(credential_path))
        digest.update(b"\0")
    return digest.hexdigest()


def _uses_registry_switch(home: Path) -> bool:
    registry_path = home / "accounts" / "registry.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return False
    if not isinstance(payload, dict):
        return False
    rows, _active = _registry_accounts(payload)
    return bool(rows)


def read_auth_state(
    codex_home: str | Path,
    *,
    account_source: str | Path | None = None,
) -> CodexAuthState:
    home = Path(codex_home).expanduser().resolve(strict=False)
    registry_path = home / "accounts" / "registry.json"
    private_payload: dict[str, Any] = {}
    registry_bytes = b""
    try:
        registry_bytes = registry_path.read_bytes()
        parsed = json.loads(registry_bytes.decode("utf-8"))
        if isinstance(parsed, dict):
            private_payload = parsed
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        pass

    source_credentials: dict[str, Path] | None = None
    source_registry: dict[str, Any] | None = None
    catalog_shared = True
    if account_source is not None:
        source_root, source_credentials, source_registry = _source_account_catalog(
            Path(account_source).expanduser()
        )
        private_accounts = home / "accounts"
        if source_registry is None:
            catalog_shared = _same_link_target(
                private_accounts,
                source_root / "accounts",
                directory=True,
            )
        else:
            catalog_shared = all(
                _same_link_target(private_accounts / f"{name}.json", source_path)
                for name, source_path in source_credentials.items()
            ) and not _registry_migration_artifacts(private_accounts)

    payload = source_registry if source_registry is not None else private_payload
    rows, source_active_key = _registry_accounts(payload)
    _private_rows, private_active_key = _registry_accounts(private_payload)
    active_key = private_active_key if source_registry is not None else source_active_key
    pointer_selection = (
        _selected_auth_pointer(home, source_credentials)
        if source_credentials is not None
        else None
    )
    if pointer_selection is not None:
        active_key = pointer_selection
    accounts: list[CodexAccount] = []
    native_names: list[str] = []
    if rows:
        for index, row in enumerate(rows):
            key = str(row.get("account_key") or row.get("key") or row.get("email") or f"account-{index + 1}").strip()
            if not key:
                continue
            accounts.append(
                CodexAccount(
                    account_key=key,
                    email=str(row.get("email") or "").strip(),
                    alias=str(row.get("alias") or "").strip(),
                    account_name=str(row.get("account_name") or row.get("name") or "").strip(),
                    active=key == active_key,
                )
            )
    else:
        native_names = (
            sorted(source_credentials, key=str.casefold)
            if source_credentials is not None
            else _native_account_names(home / "accounts")
        )
        active_key = _native_selected_account(home, native_names)
        if active_key is None and pointer_selection is not None:
            active_key = pointer_selection
        accounts = [
            CodexAccount(
                account_key=name,
                alias=name,
                account_name=name,
                active=name == active_key,
            )
            for name in native_names
        ]
    active = next((account for account in accounts if account.active), None)
    # Marker metadata remains observable, but cannot stand in for credentials
    # actually read by Codex, including a regular file replaced on refresh.
    from .codex_credentials import observe
    observed_credentials = source_credentials if source_credentials is not None else {
        account.account_key: (home / "accounts" / f"{account.account_key}.json").resolve(strict=False)
        for account in accounts
    }
    marker_key = private_active_key if rows else _native_current_account(home, list(observed_credentials))
    observation = observe(home, observed_credentials)
    active_key = observation.key
    accounts = [CodexAccount(account.account_key, account.email, account.alias, account.account_name,
                             account.account_key == active_key) for account in accounts]
    active = next((account for account in accounts if account.active), None)
    canonical = observe(source_root, observed_credentials) if account_source is not None else None
    canonical_account = next((account for account in accounts if canonical and account.account_key == canonical.key), None)
    fingerprint_names = native_names or [account.account_key for account in accounts]
    if source_registry is not None:
        registry_bytes += b"\0source-registry\0" + json.dumps(
            source_registry,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    fingerprint = _state_fingerprint(
        home,
        registry_bytes,
        fingerprint_names,
        credential_paths=source_credentials,
    )
    auth_path = home / "auth.json"
    pointer_ready = (
        auth_path.is_symlink() and pointer_selection == active_key
        if active_key is not None
        else not auth_path.exists() and not auth_path.is_symlink()
    )
    canonical_binding_ready = (
        catalog_shared
        and observation.status in {"current", "unselected"}
        and not (home / ".termdeck-accounts-migration").exists()
        and pointer_ready
        and _private_current_marker_safe(home)
        and (
            source_registry is None
            or private_active_key == pointer_selection
        )
    )
    return CodexAuthState(
        codex_home=str(home),
        accounts=tuple(accounts),
        selected_account=active.label if active else None,
        selected_account_key=active.account_key if active else None,
        fingerprint=fingerprint,
        codex_auth_available=_real_codex_auth(home) is not None,
        catalog_shared=catalog_shared,
        canonical_binding_ready=canonical_binding_ready,
        canonical_account_key=canonical.key if canonical else None,
        canonical_account=canonical_account.label if canonical_account else None,
        marker_account_key=marker_key,
        credential_status=observation.status,
        canonical_credential_status=canonical.status if canonical else "unknown",
        selection_conflict=marker_key is not None and marker_key != active_key,
        effective_fingerprint=observation.credential.digest if observation.credential else observation.status,
    )


def resolve_account(state: CodexAuthState, query: str) -> CodexAccount:
    needle = query.strip().casefold()
    if not needle:
        raise CodexAuthError("account selector cannot be empty")
    matches = []
    for account in state.accounts:
        candidates = {
            account.account_key,
            account.email,
            account.alias,
            account.account_name,
            account.label,
        }
        if any(candidate and candidate.casefold() == needle for candidate in candidates):
            matches.append(account)
    if not matches:
        raise CodexAuthError(f"saved Codex account not found: {query}")
    unique = {account.account_key: account for account in matches}
    if len(unique) != 1:
        raise CodexAuthError(f"Codex account selector is ambiguous: {query}")
    return next(iter(unique.values()))


class CodexAuthManager:
    def __init__(self, *, account_source: str | Path | None = None, timeout: float = 30.0):
        self.account_source = Path(account_source).expanduser() if account_source else default_account_source()
        self.timeout = max(1.0, float(timeout))

    def state(self, record: SessionRecord) -> CodexAuthState:
        if not record.codex_home:
            raise CodexAuthError(f"terminal {record.id} has no private CODEX_HOME")
        # Selector polling must never rewrite a live scope. Creation and the
        # stopped-only migration path prepare it; state inspection only reads
        # private selection markers and the canonical catalogue.
        return read_auth_state(
            record.codex_home,
            account_source=self.account_source,
        )

    def runtime_state(self, record: SessionRecord, state: CodexAuthState) -> dict:
        from .codex_credentials import runtime_observation
        return runtime_observation(Path(record.codex_home), record.auth_scope_id,
                                   state.effective_fingerprint or None,
                                   record_status=record.status, record_pid=record.last_pid)

    def record_activation(self, record: SessionRecord, state: CodexAuthState, activation) -> None:
        from .codex_credentials import activation_receipt
        activation_receipt(Path(record.codex_home), record.auth_scope_id,
                           state.selected_account_key, activation.pid,
                           kind=activation.credential_lifecycle,
                           expected_digest=state.effective_fingerprint or None)

    def migrate_catalog(self, record: SessionRecord) -> CodexAuthState:
        """Bind a legacy scope to the canonical catalogue while it is stopped.

        Callers must first prove that no process can read or write this private
        auth scope.  Keeping that lifecycle proof outside the credential layer
        lets selector polling remain strictly read-only for existing scopes.
        """

        if not record.codex_home:
            raise CodexAuthError(f"terminal {record.id} has no private CODEX_HOME")
        from .codex_credentials import CredentialError, sync_quiescent
        source, credentials, _ = _source_account_catalog(self.account_source)
        try:
            sync_quiescent(Path(record.codex_home), source, credentials)
        except CredentialError as exc:
            raise CodexAuthError(str(exc)) from exc
        prepare_auth_scope(
            record.codex_home,
            account_source=self.account_source,
            migrate_account_catalog=True,
        )
        state = read_auth_state(
            record.codex_home,
            account_source=self.account_source,
        )
        if not state.catalog_shared:
            raise CodexAuthError(
                f"terminal {record.id} account catalogue did not bind to the configured source"
            )
        return state

    def _restore_key(
        self,
        record: SessionRecord,
        account_key: str | None,
    ) -> CodexAuthState:
        home = Path(record.codex_home or "").expanduser().resolve(strict=False)
        if account_key is None:
            _clear_private_selection(home)
        else:
            _select_private_account(home, account_key)
        return self.state(record)

    def restore(
        self,
        record: SessionRecord,
        account_key: str | None,
    ) -> CodexSwitchResult:
        """Restore a previously selected account in one private auth scope.

        ``account_key`` is deliberately an exact key rather than a display
        label or general account query. Callers use the key captured before a
        switch, while ``None`` restores the scope to its unselected state.
        The canonical account source is never changed.
        """

        state = self.state(record)
        target_key = account_key.strip() if account_key is not None else None
        if account_key is not None and not target_key:
            return CodexSwitchResult(
                False,
                state,
                "previous Codex account key cannot be empty",
            )
        if state.selected_account_key == target_key and state.canonical_binding_ready:
            return CodexSwitchResult(True, state)
        if not state.canonical_binding_ready:
            try:
                state = self.migrate_catalog(record)
            except CodexAuthError as exc:
                return CodexSwitchResult(False, self.state(record), str(exc))
        if target_key is None:
            try:
                restored = self._restore_key(record, None)
            except CodexAuthError as exc:
                return CodexSwitchResult(False, self.state(record), str(exc))
            if restored.selected_account_key is None:
                return CodexSwitchResult(True, restored)
            return CodexSwitchResult(
                False,
                restored,
                "private Codex account state did not return to an unselected state",
            )

        account = next(
            (row for row in state.accounts if row.account_key == target_key),
            None,
        )
        if account is None:
            return CodexSwitchResult(
                False,
                state,
                f"previous saved Codex account not found: {target_key}",
            )
        try:
            restored = self._restore_key(record, account.account_key)
        except CodexAuthError as exc:
            return CodexSwitchResult(False, self.state(record), str(exc))
        if (
            restored.selected_account_key == target_key
            and restored.canonical_binding_ready
        ):
            return CodexSwitchResult(True, restored)
        return CodexSwitchResult(
            False,
            restored,
            "private Codex account state did not verify the restored canonical binding",
        )

    def switch(self, record: SessionRecord, account_query: str) -> CodexSwitchResult:
        state = self.state(record)
        try:
            account = resolve_account(state, account_query)
        except CodexAuthError as exc:
            return CodexSwitchResult(False, state, str(exc))
        previous_key = state.selected_account_key
        if not state.canonical_binding_ready:
            try:
                state = self.migrate_catalog(record)
                account = resolve_account(state, account.account_key)
            except CodexAuthError as exc:
                return CodexSwitchResult(False, self.state(record), str(exc))
        if (
            state.selected_account_key == account.account_key
            and state.canonical_binding_ready
        ):
            return CodexSwitchResult(True, state)
        try:
            _select_private_account(Path(state.codex_home), account.account_key)
            verified = self.state(record)
        except CodexAuthError as exc:
            try:
                rolled_back = self._restore_key(record, previous_key)
            except CodexAuthError:
                rolled_back = self.state(record)
            detail = str(exc)
            if rolled_back.selected_account_key != previous_key:
                detail = f"{detail}; rollback could not restore the previous account"
            return CodexSwitchResult(False, rolled_back, detail)
        if (
            verified.selected_account_key == account.account_key
            and verified.canonical_binding_ready
        ):
            return CodexSwitchResult(True, verified)

        try:
            rolled_back = self._restore_key(record, previous_key)
        except CodexAuthError:
            rolled_back = self.state(record)
        detail = "private Codex account state did not verify the requested canonical binding"
        if rolled_back.selected_account_key != previous_key:
            detail = f"{detail}; rollback could not restore the previous account"
        return CodexSwitchResult(False, rolled_back, detail)
