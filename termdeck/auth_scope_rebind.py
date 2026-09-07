"""Safely rebind legacy Codex auth scopes to managed SSD state.

An auth scope is part of the process boundary for a terminal.  Moving one
while a pane can still use it would make credentials and the native Codex
state race with a second process.  This module therefore owns only the
quiescent, exact-resume case.  It creates a fresh scope with the old scope id,
keeps conversation roots shared with the canonical Codex home, and changes
the TermDeck registry with a compare-and-swap guarded by the recovery claim.

The old scope is deliberately read-only from this module.  In particular,
there is no copy, rename, unlink, or recursive removal operation against the
old home.  An interrupted or losing transaction may leave an unused new scope
for later maintenance; it must never destroy evidence from the old scope.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import stat
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .codex_auth import (
    CodexAuthError,
    CodexAuthManager,
    default_account_source,
    ensure_private_codex_state,
    prepare_auth_scope,
    read_auth_state,
    resolve_account,
)
from .models import SessionRecord
from .resources import safe_codex_recovery_command
from .session_store import default_auth_root, managed_state_dir

if TYPE_CHECKING:
    from .session_store import SessionStore


AUTH_SCOPE_REBIND_SCHEMA = "termdeck.auth-scope-rebind.v1"
AUTH_SCOPE_REBIND_RECEIPT_DIRNAME = "auth-scope-rebind-receipts"
_AUTH_SCOPE_ID = re.compile(r"auth-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SPARSE_TABLES = (
    "threads",
    "thread_dynamic_tools",
    "thread_spawn_edges",
    "remote_control_enrollments",
    "external_agent_config_imports",
)


class AuthScopeRebindError(RuntimeError):
    """Raised when a rebind cannot satisfy its safety contract."""


@dataclass(frozen=True)
class AuthScopeRebindProof:
    """Independent lifecycle evidence required before touching SSD state."""

    missing: bool = False
    dead: bool = False
    quiescent: bool = False

    @property
    def complete(self) -> bool:
        return self.missing and self.dead and self.quiescent


@dataclass(frozen=True)
class AuthScopeRebindClaim:
    """Durable recovery claim material supplied by the coordinator."""

    claim_token: str
    owner_id: str | None = None
    target_keys: tuple[str, ...] = ()
    session_id: str | None = None

    @property
    def token(self) -> str:
        return self.claim_token


@dataclass(frozen=True)
class AuthScopeRebindResult:
    """JSON-ready outcome of a guarded auth-scope rebind."""

    session_id: str
    state: str
    detail: str
    old_home: str | None = None
    new_home: str | None = None
    auth_scope_id: str | None = None
    selected_account: str | None = None
    selected_account_key: str | None = None
    recovery_command: str | None = None
    recovery_cwd: str | None = None
    receipt_path: str | None = None
    changed: bool = False

    @property
    def ok(self) -> bool:
        return self.state in {"rebound", "already-rebound"}

    @property
    def rebound(self) -> bool:
        return self.ok

    @property
    def deferred(self) -> bool:
        return self.state == "deferred"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["ok"] = self.ok
        payload["rebound"] = self.rebound
        payload["deferred"] = self.deferred
        return payload

    to_dict = as_dict


def _result(
    record: object,
    state: str,
    detail: str,
    *,
    old_home: Path | None = None,
    new_home: Path | None = None,
    auth_scope_id: str | None = None,
    selected_account: str | None = None,
    selected_account_key: str | None = None,
    recovery_command: str | None = None,
    recovery_cwd: str | None = None,
    receipt_path: Path | None = None,
    changed: bool = False,
) -> AuthScopeRebindResult:
    return AuthScopeRebindResult(
        session_id=str(getattr(record, "id", "") or ""),
        state=state,
        detail=detail,
        old_home=str(old_home) if old_home is not None else None,
        new_home=str(new_home) if new_home is not None else None,
        auth_scope_id=auth_scope_id,
        selected_account=selected_account,
        selected_account_key=selected_account_key,
        recovery_command=recovery_command,
        recovery_cwd=recovery_cwd,
        receipt_path=str(receipt_path) if receipt_path is not None else None,
        changed=changed,
    )


def _defer(record: object, detail: str, **kwargs: object) -> AuthScopeRebindResult:
    return _result(record, "deferred", detail, **kwargs)  # type: ignore[arg-type]


def _path(value: object, *, field: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise AuthScopeRebindError(f"{field} must be an absolute path")
    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        raise AuthScopeRebindError(f"{field} is invalid")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise AuthScopeRebindError(f"{field} must be absolute")
    return Path(os.path.abspath(os.fspath(candidate)))


def _safe_scope_id(record: object) -> str:
    scope_id = str(getattr(record, "auth_scope_id", "") or "").strip()
    if _AUTH_SCOPE_ID.fullmatch(scope_id) is None:
        raise AuthScopeRebindError("auth scope id is not a managed scope id")
    return scope_id


def _safe_session_id(record: object) -> str:
    session_id = str(getattr(record, "id", "") or "").strip()
    if _SAFE_SESSION_ID.fullmatch(session_id) is None:
        raise AuthScopeRebindError("session id is not safe for a rebind receipt")
    return session_id


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise AuthScopeRebindError(f"managed SSD directory is unavailable: {path}") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuthScopeRebindError(f"managed SSD directory cannot be inspected: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AuthScopeRebindError(f"managed SSD directory is unsafe: {path}")


def _old_home_state(path: Path) -> str:
    """Return ``missing`` or validate an existing old scope read-only."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise AuthScopeRebindError(f"old auth scope cannot be inspected: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise AuthScopeRebindError(f"old auth scope is unsafe: {path}")
    return "present"


def _resolve_existing(path: Path, *, field: str) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthScopeRebindError(f"{field} cannot be resolved: {path}") from exc


def _device(path: Path, *, field: str) -> int:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise AuthScopeRebindError(f"{field} cannot be inspected: {path}") from exc
    return int(metadata.st_dev)


def _managed_paths(
    *,
    auth_root: str | Path | None,
    environ: Mapping[str, str] | None,
) -> tuple[Path, Path, Path]:
    values = os.environ if environ is None else environ
    raw_managed = str(values.get("T1000_MANAGED_ROOT", "") or "").strip()
    if not raw_managed:
        raise AuthScopeRebindError(
            "T1000_MANAGED_ROOT is required for an SSD auth-scope rebind"
        )
    try:
        state_dir = managed_state_dir(environ=values)
        root = (
            Path(auth_root).expanduser()
            if auth_root is not None
            else default_auth_root(environ=values)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthScopeRebindError("managed SSD auth root cannot be resolved") from exc
    root = _path(root, field="auth root")
    state_dir = _path(state_dir, field="managed state root")
    if raw_managed:
        managed_root = _path(raw_managed, field="T1000_MANAGED_ROOT")
        _private_directory(managed_root)
        storage_root = managed_root.parent
    else:
        raise AuthScopeRebindError(
            "T1000_MANAGED_ROOT is required for an SSD auth-scope rebind"
        )
    _private_directory(storage_root)
    _private_directory(state_dir, create=True)
    _private_directory(root, create=True)
    if root.parent != state_dir:
        # An explicit test/dependency-injected root may live below another
        # private state directory, but it must not escape the managed storage
        # cell.  ``relative_to`` rejects sibling legacy state roots.
        try:
            root.relative_to(storage_root)
        except ValueError as exc:
            raise AuthScopeRebindError(
                "auth root is outside the managed SSD storage root"
            ) from exc
    if _device(root, field="auth root") != _device(storage_root, field="managed SSD storage"):
        raise AuthScopeRebindError("auth root is not on the managed SSD device")
    return state_dir, root, storage_root


def _exact_native_resume(record: object) -> tuple[str, str, str] | None:
    raw = str(getattr(record, "recovery_command", "") or "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        return None
    if not argv or argv[0] != "codex":
        return None
    if any(str(token).casefold() == "codex-opencode" for token in argv):
        return None
    try:
        resume_index = argv.index("resume")
    except ValueError:
        return None
    if resume_index <= 0 or len(argv) != resume_index + 2:
        return None
    raw_id = argv[-1]
    try:
        session_uuid = str(UUID(raw_id))
    except (ValueError, TypeError, AttributeError):
        return None
    if raw_id != session_uuid:
        return None
    canonical = safe_codex_recovery_command(argv, session_id=session_uuid)
    if canonical is None or " resume " not in f" {canonical} ":
        return None
    command = str(getattr(record, "command", "") or "").strip()
    if command:
        try:
            command_argv = shlex.split(command)
        except ValueError:
            return None
        if any(Path(str(token)).name.casefold() == "codex-opencode" for token in command_argv):
            return None
    recovery_cwd = str(
        getattr(record, "recovery_cwd", None)
        or getattr(record, "cwd", "")
        or ""
    ).strip()
    if not recovery_cwd:
        return None
    try:
        cwd = _path(recovery_cwd, field="recovery cwd")
    except AuthScopeRebindError:
        return None
    return canonical, session_uuid, str(cwd)


def _mapping_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _claim_values(
    claim: object | None,
    *,
    claim_token: str | None,
    claim_owner: str | None,
    claim_target_keys: tuple[str, ...] | list[str] | None,
) -> AuthScopeRebindClaim | None:
    token = claim_token or _mapping_value(claim, "claim_token") or _mapping_value(claim, "token")
    if token is None or not str(token).strip():
        return None
    owner = claim_owner
    if owner is None:
        raw_owner = _mapping_value(claim, "owner_id")
        owner = str(raw_owner).strip() if raw_owner is not None else None
    keys = claim_target_keys
    if keys is None:
        raw_keys = _mapping_value(claim, "target_keys", ())
        keys = tuple(str(key) for key in raw_keys) if raw_keys else ()
    else:
        keys = tuple(str(key) for key in keys)
    raw_session = _mapping_value(claim, "session_id")
    return AuthScopeRebindClaim(
        claim_token=str(token).strip(),
        owner_id=owner or None,
        target_keys=tuple(key for key in keys if key),
        session_id=str(raw_session).strip() if raw_session else None,
    )


def _row_value(row: sqlite3.Row | tuple[Any, ...], key: str, index: int) -> object:
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _verify_claim(
    store: SessionStore,
    record: object,
    claim: AuthScopeRebindClaim,
    *,
    now: float,
) -> bool:
    connection = getattr(store, "conn", None)
    if connection is None:
        return False
    session_id = str(getattr(record, "id", "") or "")
    try:
        rows = connection.execute(
            """
            SELECT target_key, session_id, owner_id, claim_token, expires_at
            FROM recovery_claims
            WHERE session_id = ? AND claim_token = ?
            """,
            (session_id, claim.claim_token),
        ).fetchall()
    except sqlite3.Error:
        return False
    if claim.session_id is not None and claim.session_id != session_id:
        return False
    if not rows:
        return False
    required = set(claim.target_keys)
    observed: set[str] = set()
    for row in rows:
        if claim.owner_id is not None and str(_row_value(row, "owner_id", 2)) != claim.owner_id:
            return False
        try:
            expires_at = float(_row_value(row, "expires_at", 4))
        except (TypeError, ValueError):
            return False
        if expires_at <= now:
            return False
        observed.add(str(_row_value(row, "target_key", 0)))
    return not required or required.issubset(observed)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise AuthScopeRebindError(f"sparse Codex state cannot be opened: {path}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _verify_sparse_state(destination: Path, canonical: Path) -> None:
    state_path = destination / "state_5.sqlite"
    try:
        if (
            state_path.is_symlink()
            or not state_path.is_file()
            or state_path.stat().st_nlink != 1
            or stat.S_IMODE(state_path.stat().st_mode) != 0o600
        ):
            raise AuthScopeRebindError("new Codex state database is not a private regular file")
    except OSError as exc:
        raise AuthScopeRebindError("new Codex state database cannot be inspected") from exc

    for name in ("sessions", "archived_sessions"):
        source_root = canonical / name
        if not source_root.is_dir():
            continue
        private_root = destination / name
        try:
            if (
                not private_root.is_symlink()
                or not os.path.samefile(private_root, source_root)
            ):
                raise AuthScopeRebindError(
                    f"new Codex {name} root is not shared with canonical state"
                )
        except OSError as exc:
            raise AuthScopeRebindError(
                f"new Codex {name} root cannot be verified"
            ) from exc

    try:
        connection = _readonly_connection(state_path)
    except AuthScopeRebindError:
        raise
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in _SPARSE_TABLES:
            if table not in tables:
                continue
            count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if count:
                raise AuthScopeRebindError(
                    f"new Codex state is not sparse; {table} contains conversation data"
                )
        if "thread_sections" in tables:
            rows = connection.execute(
                "SELECT id, name FROM thread_sections"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][1]) != "Pinned":
                raise AuthScopeRebindError(
                    "new Codex state does not contain only the built-in Pinned section"
                )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).casefold() != "ok":
            raise AuthScopeRebindError("new Codex state failed SQLite integrity checks")
    except AuthScopeRebindError:
        raise
    except sqlite3.Error as exc:
        raise AuthScopeRebindError("new Codex sparse state cannot be verified") from exc
    finally:
        connection.close()


def _verify_canonical_binding(destination: Path, canonical: Path) -> None:
    binding = destination / ".termdeck-canonical-home"
    if (
        not binding.is_symlink()
        or binding.resolve(strict=True) != canonical
        or not os.path.samefile(binding, canonical)
    ):
        raise AuthScopeRebindError("new Codex scope has no canonical-home binding")
    for name in ("prompts",):
        source = canonical / name
        if source.is_dir():
            target = destination / name
            if not target.is_symlink() or not os.path.samefile(target, source):
                raise AuthScopeRebindError(
                    f"new Codex {name} asset is not a canonical shared link"
                )


def _selected_account(
    record: object,
    old_home: Path,
    canonical: Path,
    old_state_status: str,
) -> tuple[str, str | None]:
    """Resolve one exact account key without copying credentials."""

    try:
        canonical_state = read_auth_state(canonical, account_source=canonical)
    except CodexAuthError as exc:
        raise AuthScopeRebindError(
            f"canonical Codex account catalogue cannot be verified: {exc}"
        ) from exc

    hinted = str(getattr(record, "selected_account", "") or "").strip() or None
    if old_state_status == "present":
        try:
            old_state = read_auth_state(old_home, account_source=canonical)
        except CodexAuthError as exc:
            raise AuthScopeRebindError(
                f"old Codex account selection cannot be verified: {exc}"
            ) from exc
        selected_key = old_state.selected_account_key
        if not selected_key:
            raise AuthScopeRebindError("old Codex account selection is unknown")
        if hinted and hinted not in {old_state.selected_account, selected_key}:
            raise AuthScopeRebindError(
                "saved Codex account metadata disagrees with the old scope"
            )
        selected_label = old_state.selected_account
    else:
        if not hinted:
            raise AuthScopeRebindError("saved Codex account selection is unknown")
        try:
            selected = resolve_account(canonical_state, hinted)
        except CodexAuthError as exc:
            raise AuthScopeRebindError(
                f"saved Codex account selection is not exact: {exc}"
            ) from exc
        selected_key = selected.account_key
        selected_label = selected.label

    if selected_key not in {account.account_key for account in canonical_state.accounts}:
        raise AuthScopeRebindError("selected Codex account is absent from canonical SSD state")
    return selected_key, selected_label


def _ensure_new_scope(
    destination: Path,
    canonical: Path,
    record: object,
    selected_key: str,
) -> tuple[str | None, str | None]:
    try:
        prepare_auth_scope(destination, account_source=canonical)
        ensure_private_codex_state(
            destination,
            account_source=canonical,
            session_source=canonical,
            require_complete=True,
        )
        target_record = replace(
            record,
            codex_home=str(destination),
        )
        restored = CodexAuthManager(account_source=canonical).restore(
            target_record,
            selected_key,
        )
        if not restored.ok:
            raise AuthScopeRebindError(
                restored.error or "new Codex scope did not restore the selected account"
            )
        state = restored.state
        if not state.canonical_binding_ready or state.selected_account_key != selected_key:
            raise AuthScopeRebindError(
                "new Codex scope failed canonical account-binding verification"
            )
        _verify_sparse_state(destination, canonical)
        _verify_canonical_binding(destination, canonical)
        return state.selected_account, state.selected_account_key
    except (CodexAuthError, OSError, sqlite3.Error) as exc:
        if isinstance(exc, AuthScopeRebindError):
            raise
        raise AuthScopeRebindError(f"new Codex scope could not be prepared safely: {exc}") from exc


def _receipt_dir(state_dir: Path) -> Path:
    directory = state_dir / AUTH_SCOPE_REBIND_RECEIPT_DIRNAME
    _private_directory(directory, create=True)
    return directory


def _write_receipt(
    state_dir: Path,
    *,
    record: object,
    old_home: Path,
    new_home: Path,
    scope_id: str,
    selected_key: str,
    selected_label: str | None,
    recovery_command: str,
    recovery_cwd: str,
    claim: AuthScopeRebindClaim,
) -> Path:
    directory = _receipt_dir(state_dir)
    session_id = _safe_session_id(record)
    destination = directory / f"{session_id}.json"
    payload = {
        "schema": AUTH_SCOPE_REBIND_SCHEMA,
        "state": "rebound",
        "created_at": time.time(),
        "session_id": session_id,
        "auth_scope_id": scope_id,
        "old_home": str(old_home),
        "new_home": str(new_home),
        "selected_account": selected_label,
        "selected_account_key": selected_key,
        "recovery_command": recovery_command,
        "recovery_cwd": recovery_cwd,
        "claim_token": claim.claim_token,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    try:
        if destination.is_symlink() or (
            destination.exists()
            and (
                not destination.is_file()
                or destination.stat().st_uid != os.geteuid()
                or stat.S_IMODE(destination.stat().st_mode) != 0o600
            )
        ):
            raise AuthScopeRebindError("existing auth-scope rebind receipt is unsafe")
        if destination.is_file() and destination.read_bytes() == encoded:
            return destination
    except OSError as exc:
        raise AuthScopeRebindError("existing auth-scope rebind receipt cannot be inspected") from exc

    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.tmp-",
            dir=directory,
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except AuthScopeRebindError:
        raise
    except OSError as exc:
        raise AuthScopeRebindError("auth-scope rebind receipt could not be made durable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def _cas_codex_home(
    store: SessionStore,
    record: object,
    *,
    expected_old: str,
    new_home: str,
    scope_id: str,
    claim: AuthScopeRebindClaim,
    now: float,
) -> tuple[bool, str]:
    connection = getattr(store, "conn", None)
    if connection is None:
        raise AuthScopeRebindError("session registry has no atomic connection")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _verify_claim(store, record, claim, now=now):
            connection.rollback()
            return False, "durable recovery claim is missing, expired, or owned by another worker"
        session_id = str(getattr(record, "id", "") or "")
        row = connection.execute(
            "SELECT codex_home, auth_scope_id, external FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            return False, "session disappeared before auth-scope CAS"
        current_home = str(_row_value(row, "codex_home", 0) or "")
        current_scope = str(_row_value(row, "auth_scope_id", 1) or "")
        if bool(_row_value(row, "external", 2)):
            connection.rollback()
            return False, "external sessions cannot be rebound"
        if current_scope != scope_id or current_home != expected_old:
            if current_scope == scope_id and current_home == new_home:
                connection.rollback()
                return True, "auth scope was already rebound by this session"
            connection.rollback()
            return False, "session auth-scope identity changed before CAS"
        conflict = connection.execute(
            "SELECT id FROM sessions WHERE codex_home = ? AND id != ? LIMIT 1",
            (new_home, session_id),
        ).fetchone()
        if conflict is not None:
            connection.rollback()
            return False, "new auth scope is already bound to another session"
        updated = connection.execute(
            """
            UPDATE sessions
            SET codex_home = ?
            WHERE id = ? AND auth_scope_id = ? AND codex_home = ? AND external = 0
            """,
            (new_home, session_id, scope_id, expected_old),
        )
        if int(updated.rowcount) != 1:
            connection.rollback()
            return False, "session auth-scope CAS lost its expected old home"
        connection.commit()
        return True, "auth scope rebound"
    except AuthScopeRebindError:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    except sqlite3.Error as exc:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise AuthScopeRebindError(f"session auth-scope CAS failed: {exc}") from exc


def rebind_auth_scope(
    record: SessionRecord | object,
    store: SessionStore,
    *,
    proof: AuthScopeRebindProof | Mapping[str, object] | object | None = None,
    claim: AuthScopeRebindClaim | Mapping[str, object] | object | None = None,
    missing: bool | None = None,
    dead: bool | None = None,
    quiescent: bool | None = None,
    claim_token: str | None = None,
    claim_owner: str | None = None,
    claim_target_keys: tuple[str, ...] | list[str] | None = None,
    expected_old_home: str | Path | None = None,
    canonical_home: str | Path | None = None,
    auth_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> AuthScopeRebindResult:
    """Rebind one missing/dead exact Codex tab to its managed SSD scope.

    ``proof`` is intentionally explicit.  A status string, a stale PID, or a
    live tmux listing cannot stand in for all three lifecycle observations.
    The coordinator must also supply the still-live durable recovery claim;
    this function rechecks that claim immediately before the CAS.
    """

    session_id = str(getattr(record, "id", "") or "")
    old_text = str(
        expected_old_home
        if expected_old_home is not None
        else getattr(record, "codex_home", "") or ""
    )
    try:
        scope_id = _safe_scope_id(record)
        session_id = _safe_session_id(record)
        old_path = _path(old_text, field="old auth scope")
        if bool(getattr(record, "external", False)):
            return _defer(record, "external sessions cannot be rebound", old_home=old_path, auth_scope_id=scope_id)
        status = str(getattr(record, "status", "") or "").strip().casefold()
        if status in {"new", "remembered", "archived"}:
            return _defer(record, "layout and archived sessions cannot be rebound", old_home=old_path, auth_scope_id=scope_id)
        exact = _exact_native_resume(record)
        if exact is None:
            return _defer(record, "only an exact native Codex resume UUID may be rebound", old_home=old_path, auth_scope_id=scope_id)
        recovery_command, _resume_id, recovery_cwd = exact
        values = os.environ if environ is None else environ
        selected_proof = proof
        def proof_value(name: str, explicit: bool | None) -> bool:
            if explicit is not None:
                return explicit is True
            raw = _mapping_value(selected_proof, name, False)
            return raw is True

        if not all(
            proof_value(name, explicit)
            for name, explicit in (
                ("missing", missing),
                ("dead", dead),
                ("quiescent", quiescent),
            )
        ):
            return _defer(record, "missing/dead/quiescent proof is incomplete", old_home=old_path, auth_scope_id=scope_id, recovery_command=recovery_command, recovery_cwd=recovery_cwd)
        durable_claim = _claim_values(
            claim,
            claim_token=claim_token,
            claim_owner=claim_owner,
            claim_target_keys=claim_target_keys,
        )
        if durable_claim is None:
            return _defer(record, "a durable recovery claim is required", old_home=old_path, auth_scope_id=scope_id, recovery_command=recovery_command, recovery_cwd=recovery_cwd)
        check_time = time.time() if now is None else float(now)
        if not _verify_claim(store, record, durable_claim, now=check_time):
            return _defer(record, "durable recovery claim is missing, expired, or owned by another worker", old_home=old_path, auth_scope_id=scope_id, recovery_command=recovery_command, recovery_cwd=recovery_cwd)
        old_state_status = _old_home_state(old_path)
        canonical_requested = (
            Path(canonical_home).expanduser()
            if canonical_home is not None
            else default_account_source()
        )
        canonical = _resolve_existing(_path(canonical_requested, field="canonical Codex home"), field="canonical Codex home")
        if not canonical.is_dir():
            raise AuthScopeRebindError("canonical Codex home is not a directory")
        state_dir, root, storage_root = _managed_paths(
            auth_root=auth_root,
            environ=values,
        )
        old_resolved = (
            _resolve_existing(old_path, field="old auth scope")
            if old_state_status == "present"
            else old_path
        )
        if old_state_status == "present" and old_resolved == canonical:
            raise AuthScopeRebindError("old auth scope aliases canonical Codex home")
        if _device(canonical, field="canonical Codex home") != _device(storage_root, field="managed SSD storage"):
            raise AuthScopeRebindError("canonical Codex home is not on the managed SSD device")
        destination = root / scope_id
        destination_aliases_old = False
        if old_state_status == "present" and (
            destination.exists() or destination.is_symlink()
        ):
            try:
                destination_aliases_old = os.path.samefile(destination, old_resolved)
            except FileNotFoundError:
                if destination.is_symlink():
                    raise AuthScopeRebindError(
                        "new auth scope is a dangling link"
                    )
        if destination == old_path or destination_aliases_old:
            raise AuthScopeRebindError("new auth scope aliases the old auth scope")
        selected_key, selected_label = _selected_account(
            record,
            old_path,
            canonical,
            old_state_status,
        )
        _ensure_new_scope(destination, canonical, record, selected_key)
        _verify_canonical_binding(destination, canonical)
        expected_old = str(old_path)
        new_text = str(destination)
        cas_ok, cas_detail = _cas_codex_home(
            store,
            record,
            expected_old=expected_old,
            new_home=new_text,
            scope_id=scope_id,
            claim=durable_claim,
            now=time.time() if now is None else check_time,
        )
        if not cas_ok:
            return _defer(record, cas_detail, old_home=old_path, new_home=destination, auth_scope_id=scope_id, selected_account=selected_label, selected_account_key=selected_key, recovery_command=recovery_command, recovery_cwd=recovery_cwd)
        receipt = _write_receipt(
            state_dir,
            record=record,
            old_home=old_path,
            new_home=destination,
            scope_id=scope_id,
            selected_key=selected_key,
            selected_label=selected_label,
            recovery_command=recovery_command,
            recovery_cwd=recovery_cwd,
            claim=durable_claim,
        )
        return _result(
            record,
            "already-rebound" if cas_detail.startswith("auth scope was already") else "rebound",
            cas_detail,
            old_home=old_path,
            new_home=destination,
            auth_scope_id=scope_id,
            selected_account=selected_label,
            selected_account_key=selected_key,
            recovery_command=recovery_command,
            recovery_cwd=recovery_cwd,
            receipt_path=receipt,
            changed=not cas_detail.startswith("auth scope was already"),
        )
    except (AuthScopeRebindError, OSError, RuntimeError, ValueError, CodexAuthError) as exc:
        detail = str(exc) or exc.__class__.__name__
        return _defer(record, detail, old_home=Path(old_text) if old_text else None, auth_scope_id=(str(getattr(record, "auth_scope_id", "") or "") or None))


class AuthScopeRebinder:
    """Small dependency-injection facade for callers and tests."""

    def __init__(self, store: SessionStore, **defaults: object) -> None:
        self.store = store
        self.defaults = dict(defaults)

    def rebind(self, record: SessionRecord | object, **kwargs: object) -> AuthScopeRebindResult:
        options = dict(self.defaults)
        options.update(kwargs)
        return rebind_auth_scope(record, self.store, **options)  # type: ignore[arg-type]


rebind_missing_auth_scope = rebind_auth_scope
