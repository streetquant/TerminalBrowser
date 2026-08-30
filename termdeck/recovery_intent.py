from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .desktop_integration import managed_autostart_ready
from .models import RECOVERY_INTENT_ACTIVE
from .session_recipe import (
    SessionRecipeError,
    automatic_recovery_allowed,
    recipe_from_record,
)
from .session_store import legacy_state_dir, utc_now


RECOVERY_INTENT_SCHEMA = "termdeck.recovery-intent.v1"
RECOVERY_INTENT_FINGERPRINT_SCHEMA = "termdeck.recovery-intent-fingerprint.v1"

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 64 * 1024
_MAX_PENDING_FILES = 256
_MAX_TEXT_BYTES = 64 * 1024
_MAX_COMMAND_BYTES = 256 * 1024
_MAX_CWD_BYTES = 16 * 1024
_RECOVERY_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending", "start-error", "stopped"}
)
_LAYOUT_STATUSES = frozenset({"new", "remembered"})
_FINGERPRINT_FIELDS = (
    "id",
    "name",
    "tag",
    "command",
    "cwd",
    "tmux_name",
    "created_at",
    "last_opened_at",
    "status",
    "last_pid",
    "external",
    "recovery_command",
    "recovery_cwd",
    "restart_policy",
)
_SIDECAR_FIELDS = frozenset(
    {
        "schema",
        "intent",
        "request_id",
        "created_at",
        "session_id",
        "name",
        "tag",
        "cwd",
        "tmux_name",
        "external",
        "fingerprint",
    }
)
_REQUIRED_SOURCE_COLUMNS = frozenset(
    set(_FINGERPRINT_FIELDS)
    - {"recovery_command", "recovery_cwd", "restart_policy"}
)
_OPTIONAL_SOURCE_DEFAULTS: dict[str, object] = {
    "recovery_command": None,
    "recovery_cwd": None,
    "restart_policy": "on-failure",
}


class RecoveryIntentError(ValueError):
    """Raised when an explicit recovery intent cannot be safely recorded."""


@dataclass(frozen=True)
class RecoveryIntentPaths:
    managed_root: Path
    state_dir: Path
    intent_dir: Path
    lock_path: Path


@dataclass(frozen=True)
class RecoveryIntentRequest:
    session_id: str
    name: str
    cwd: str
    request_id: str
    created_at: str
    fingerprint: str
    queued: bool = True
    idempotent: bool = False
    tag: str = ""
    tmux_name: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "queued": self.queued,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True)
class RecoveryIntentOutcome:
    session_id: str
    state: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "detail": self.detail,
        }


def recovery_intent_paths(
    *, environ: Mapping[str, str] | None = None
) -> RecoveryIntentPaths:
    values = os.environ if environ is None else environ
    raw_root = str(values.get("T1000_MANAGED_ROOT", "") or "").strip()
    if not raw_root:
        raise RecoveryIntentError(
            "T1000_MANAGED_ROOT is required; refusing legacy recovery-intent state"
        )
    if "\x00" in raw_root:
        raise RecoveryIntentError("T1000_MANAGED_ROOT contains NUL")
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise RecoveryIntentError("T1000_MANAGED_ROOT must be an absolute path")
    state_dir = root.parent / "termdeck"
    return RecoveryIntentPaths(
        managed_root=root,
        state_dir=state_dir,
        intent_dir=state_dir / "recovery-intents",
        lock_path=state_dir / ".recovery-intents.lock",
    )


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise RecoveryIntentError("current user identity is unavailable")
    return int(getuid())


def _mode(path_stat: os.stat_result) -> int:
    return stat.S_IMODE(path_stat.st_mode)


def _ensure_managed_root(paths: RecoveryIntentPaths) -> None:
    ready, detail = managed_autostart_ready(managed_root=paths.managed_root)
    if not ready:
        raise RecoveryIntentError(f"managed SSD state is not ready: {detail}")
    try:
        root_stat = paths.managed_root.lstat()
        storage_stat = paths.managed_root.parent.lstat()
    except OSError as exc:
        raise RecoveryIntentError("managed SSD state cannot be inspected") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RecoveryIntentError("managed root is not a real directory")
    if stat.S_ISLNK(storage_stat.st_mode) or not stat.S_ISDIR(storage_stat.st_mode):
        raise RecoveryIntentError("managed storage root is not a real directory")
    uid = _current_uid()
    if root_stat.st_uid != uid or storage_stat.st_uid != uid:
        raise RecoveryIntentError("managed recovery state is not owned by the current user")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise RecoveryIntentError(f"cannot open recovery-intent directory: {path}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise RecoveryIntentError(f"cannot durably update recovery-intent directory: {path}") from exc
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    created = False
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise RecoveryIntentError(f"cannot create private recovery directory: {path}") from exc
        created = True
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise RecoveryIntentError(f"cannot inspect private recovery directory: {path}") from exc
    except OSError as exc:
        raise RecoveryIntentError(f"cannot inspect private recovery directory: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RecoveryIntentError(f"recovery state path is not a real directory: {path}")
    if path_stat.st_uid != _current_uid() or _mode(path_stat) != 0o700:
        raise RecoveryIntentError(f"recovery directory is not private: {path}")
    if created:
        _fsync_directory(path.parent)


def _prepare_private_paths(paths: RecoveryIntentPaths) -> None:
    _ensure_managed_root(paths)
    _ensure_private_directory(paths.state_dir)
    _ensure_private_directory(paths.intent_dir)


@contextmanager
def _intent_lock(paths: RecoveryIntentPaths) -> Iterator[None]:
    try:
        descriptor = os.open(
            paths.lock_path,
            os.O_CREAT
            | os.O_RDWR
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise RecoveryIntentError("cannot open recovery-intent lock") from exc
    try:
        path_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_uid != _current_uid()
            or _mode(path_stat) != 0o600
        ):
            raise RecoveryIntentError("recovery-intent lock is not a private regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise RecoveryIntentError("cannot acquire recovery-intent lock") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _bounded_text(value: object, *, field: str, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RecoveryIntentError(f"recovery-intent {field} must be text")
    if "\x00" in value:
        raise RecoveryIntentError(f"recovery-intent {field} contains NUL")
    if not empty and not value:
        raise RecoveryIntentError(f"recovery-intent {field} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise RecoveryIntentError(f"recovery-intent {field} is too large")
    return value


def _validate_session_id(value: object) -> str:
    session_id = _bounded_text(value, field="session_id", maximum=128)
    if _SESSION_ID.fullmatch(session_id) is None:
        raise RecoveryIntentError("recovery-intent session_id is invalid")
    return session_id


def _canonical_json(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RecoveryIntentError("recovery-intent JSON is not encodable") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise RecoveryIntentError("recovery-intent JSON is too large")
    return encoded


def _fingerprint_value(field: str, value: object) -> object:
    if field in {"external"}:
        if isinstance(value, bool):
            return int(value)
        if type(value) is not int or value not in (0, 1):
            raise RecoveryIntentError("session external marker is invalid")
        return value
    if field == "last_pid":
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise RecoveryIntentError("session process marker is invalid")
        return value
    if field == "restart_policy":
        value = str(value or "on-failure")
    if field in {"command"}:
        maximum = _MAX_COMMAND_BYTES
    elif field in {"cwd", "recovery_cwd"}:
        maximum = _MAX_CWD_BYTES
    else:
        maximum = _MAX_TEXT_BYTES
    if value is None and field in {"recovery_command", "recovery_cwd"}:
        return None
    return _bounded_text(value, field=field, maximum=maximum, empty=field in {"recovery_command", "recovery_cwd"})


def _fingerprint_from_values(values: Mapping[str, object]) -> str:
    fields = {
        field: _fingerprint_value(field, values.get(field))
        for field in _FINGERPRINT_FIELDS
    }
    encoded = _canonical_json(
        {
            "schema": RECOVERY_INTENT_FINGERPRINT_SCHEMA,
            "fields": fields,
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_database_path() -> Path:
    candidate = Path(legacy_state_dir()).expanduser()
    source = candidate / "sessions.sqlite3"
    try:
        path_stat = source.lstat()
    except OSError as exc:
        raise RecoveryIntentError("legacy session database is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryIntentError("legacy session database is not a regular file")
    return source.resolve(strict=True)


def _read_source_row(
    session_id: str,
    *,
    expected_name: str,
    expected_cwd: str,
) -> tuple[dict[str, object], str]:
    source = _source_database_path()
    try:
        connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise RecoveryIntentError("legacy session database cannot be opened read-only") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if not query_only or int(query_only[0]) != 1:
            raise RecoveryIntentError("legacy session database is not query-only")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if not _REQUIRED_SOURCE_COLUMNS.issubset(columns):
            missing = sorted(_REQUIRED_SOURCE_COLUMNS - columns)
            raise RecoveryIntentError(
                "legacy session database lacks recovery identity fields: "
                + ", ".join(missing)
            )
        before = int(connection.execute("PRAGMA data_version").fetchone()[0])
        selected_fields = [
            field for field in _FINGERPRINT_FIELDS if field in columns
        ]
        selected = ", ".join(selected_fields)
        row = connection.execute(
            f"SELECT {selected} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        if before != after:
            raise RecoveryIntentError(
                "legacy session database changed during recovery-intent request"
            )
        if row is None:
            raise RecoveryIntentError("legacy session record was not found")
        values = {
            field: (
                row[field]
                if field in columns
                else _OPTIONAL_SOURCE_DEFAULTS[field]
            )
            for field in _FINGERPRINT_FIELDS
        }
    except RecoveryIntentError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise RecoveryIntentError("legacy session record cannot be read safely") from exc
    finally:
        connection.close()

    actual_id = _validate_session_id(values["id"])
    if actual_id != session_id:
        raise RecoveryIntentError("legacy session identity changed during request")
    name = _bounded_text(values["name"], field="name", maximum=_MAX_TEXT_BYTES)
    cwd = _bounded_text(values["cwd"], field="cwd", maximum=_MAX_CWD_BYTES)
    if name != expected_name or cwd != expected_cwd:
        raise RecoveryIntentError("legacy session name or cwd does not match the request")
    if type(values["external"]) is not int or values["external"] != 0:
        raise RecoveryIntentError("external sessions cannot receive recovery intent")
    status = _bounded_text(values["status"], field="status", maximum=128).strip().casefold()
    if status in _LAYOUT_STATUSES or status == "archived":
        raise RecoveryIntentError("layout and archived sessions cannot receive recovery intent")
    for field in ("tag", "tmux_name", "created_at", "last_opened_at"):
        _bounded_text(values[field], field=field, maximum=_MAX_TEXT_BYTES)
    fingerprint = _fingerprint_from_values(values)
    return values, fingerprint


def _sidecar_payload(request: RecoveryIntentRequest, values: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": RECOVERY_INTENT_SCHEMA,
        "intent": RECOVERY_INTENT_ACTIVE,
        "request_id": request.request_id,
        "created_at": request.created_at,
        "session_id": request.session_id,
        "name": request.name,
        "tag": _bounded_text(values["tag"], field="tag", maximum=_MAX_TEXT_BYTES),
        "cwd": request.cwd,
        "tmux_name": _bounded_text(values["tmux_name"], field="tmux_name", maximum=_MAX_TEXT_BYTES),
        "external": 0,
        "fingerprint": request.fingerprint,
    }


def _decode_json_object(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryIntentError("recovery-intent file is not UTF-8") from exc

    def reject_duplicates(pairs):
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryIntentError("recovery-intent JSON has duplicate fields")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecoveryIntentError(f"recovery-intent JSON constant is invalid: {value}")
            ),
        )
    except RecoveryIntentError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryIntentError("recovery-intent JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise RecoveryIntentError("recovery-intent JSON must be an object")
    if _canonical_json(payload) != text:
        raise RecoveryIntentError("recovery-intent JSON is not canonical")
    return payload


def _read_file_bounded(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent file cannot be opened safely") from exc
    try:
        path_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_uid != _current_uid()
            or _mode(path_stat) != 0o600
        ):
            raise RecoveryIntentError("recovery-intent file is not a private regular file")
        if path_stat.st_size > _MAX_JSON_BYTES:
            raise RecoveryIntentError("recovery-intent file is too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(16 * 1024, _MAX_JSON_BYTES - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise RecoveryIntentError("recovery-intent file is too large")
        return b"".join(chunks)
    except RecoveryIntentError:
        raise
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent file cannot be read safely") from exc
    finally:
        os.close(descriptor)


def _read_sidecar(path: Path) -> RecoveryIntentRequest:
    if path.name != f"{path.stem}.json" or _SESSION_ID.fullmatch(path.stem) is None:
        raise RecoveryIntentError("recovery-intent filename is invalid")
    payload = _decode_json_object(_read_file_bounded(path))
    if set(payload) != _SIDECAR_FIELDS:
        raise RecoveryIntentError("recovery-intent fields are invalid")
    if payload.get("schema") != RECOVERY_INTENT_SCHEMA:
        raise RecoveryIntentError("recovery-intent schema is unsupported")
    if payload.get("intent") != RECOVERY_INTENT_ACTIVE:
        raise RecoveryIntentError("recovery-intent value is not active")
    session_id = _validate_session_id(payload.get("session_id"))
    if session_id != path.stem:
        raise RecoveryIntentError("recovery-intent filename does not match its session")
    name = _bounded_text(payload.get("name"), field="name", maximum=_MAX_TEXT_BYTES)
    cwd = _bounded_text(payload.get("cwd"), field="cwd", maximum=_MAX_CWD_BYTES)
    if not Path(cwd).is_absolute():
        raise RecoveryIntentError("recovery-intent cwd must be absolute")
    tag = _bounded_text(payload.get("tag"), field="tag", maximum=_MAX_TEXT_BYTES)
    tmux_name = _bounded_text(
        payload.get("tmux_name"), field="tmux_name", maximum=_MAX_TEXT_BYTES
    )
    created_at = _bounded_text(
        payload.get("created_at"), field="created_at", maximum=_MAX_TEXT_BYTES
    )
    request_id = _bounded_text(
        payload.get("request_id"), field="request_id", maximum=64
    )
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise RecoveryIntentError("recovery-intent request_id is invalid") from exc
    fingerprint = _bounded_text(
        payload.get("fingerprint"), field="fingerprint", maximum=64
    )
    if _SHA256.fullmatch(fingerprint) is None:
        raise RecoveryIntentError("recovery-intent fingerprint is invalid")
    if type(payload.get("external")) is not int or payload.get("external") != 0:
        raise RecoveryIntentError("recovery-intent external marker is invalid")
    return RecoveryIntentRequest(
        session_id=session_id,
        name=name,
        cwd=cwd,
        request_id=request_id,
        created_at=created_at,
        fingerprint=fingerprint,
        tag=tag,
        tmux_name=tmux_name,
    )


def _write_sidecar(path: Path, payload: Mapping[str, object]) -> None:
    encoded = _canonical_json(dict(payload)).encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise RecoveryIntentError("recovery-intent file is too large")
    temporary_fd = -1
    temporary_path: Path | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(temporary_fd, 0o600)
        written = 0
        while written < len(encoded):
            count = os.write(temporary_fd, encoded[written:])
            if count <= 0:
                raise RecoveryIntentError("recovery-intent file write made no progress")
            written += count
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise RecoveryIntentError("cannot inspect existing recovery intent") from exc
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise RecoveryIntentError("existing recovery intent is not a regular file")
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    except RecoveryIntentError:
        raise
    except OSError as exc:
        raise RecoveryIntentError("cannot durably publish recovery intent") from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _request_from_existing(
    existing: RecoveryIntentRequest,
    *,
    session_id: str,
    name: str,
    cwd: str,
) -> RecoveryIntentRequest:
    if (
        existing.session_id != session_id
        or existing.name != name
        or existing.cwd != cwd
    ):
        raise RecoveryIntentError("a different recovery intent is already pending")
    return RecoveryIntentRequest(
        session_id=existing.session_id,
        name=existing.name,
        cwd=existing.cwd,
        request_id=existing.request_id,
        created_at=existing.created_at,
        fingerprint=existing.fingerprint,
        idempotent=True,
        tag=existing.tag,
        tmux_name=existing.tmux_name,
    )


def request_recovery_intent(
    session_id: str,
    *,
    name: str,
    cwd: str,
    environ: Mapping[str, str] | None = None,
) -> RecoveryIntentRequest:
    session_id = _validate_session_id(session_id)
    name = _bounded_text(name, field="name", maximum=_MAX_TEXT_BYTES)
    cwd = _bounded_text(cwd, field="cwd", maximum=_MAX_CWD_BYTES)
    if not Path(cwd).is_absolute():
        raise RecoveryIntentError("recovery-intent cwd must be absolute")
    paths = recovery_intent_paths(environ=environ)
    _prepare_private_paths(paths)
    sidecar_path = paths.intent_dir / f"{session_id}.json"
    with _intent_lock(paths):
        try:
            existing = _read_sidecar(sidecar_path)
        except FileNotFoundError:
            existing = None
        except RecoveryIntentError:
            if sidecar_path.exists() or sidecar_path.is_symlink():
                raise
            existing = None
        if existing is not None:
            return _request_from_existing(
                existing,
                session_id=session_id,
                name=name,
                cwd=cwd,
            )
        values, fingerprint = _read_source_row(
            session_id,
            expected_name=name,
            expected_cwd=cwd,
        )
        request = RecoveryIntentRequest(
            session_id=session_id,
            name=name,
            cwd=cwd,
            request_id=str(uuid.uuid4()),
            created_at=utc_now(),
            fingerprint=fingerprint,
            tag=_bounded_text(values["tag"], field="tag", maximum=_MAX_TEXT_BYTES),
            tmux_name=_bounded_text(
                values["tmux_name"], field="tmux_name", maximum=_MAX_TEXT_BYTES
            ),
        )
        _write_sidecar(sidecar_path, _sidecar_payload(request, values))
        return request


class _IntentMismatch(Exception):
    pass


def _validate_current_record(
    current: object,
    request: RecoveryIntentRequest,
) -> None:
    if current is None:
        raise _IntentMismatch("session is no longer present in the managed registry")
    if str(getattr(current, "id", "")) != request.session_id:
        raise _IntentMismatch("session identity does not match the pending intent")
    if getattr(current, "name", None) != request.name or getattr(current, "cwd", None) != request.cwd:
        raise _IntentMismatch("session name or cwd does not match the pending intent")
    if (
        getattr(current, "tag", None) != request.tag
        or getattr(current, "tmux_name", None) != request.tmux_name
    ):
        raise _IntentMismatch("session tag or tmux identity does not match the pending intent")
    if getattr(current, "external", True):
        raise _IntentMismatch("external sessions cannot receive recovery intent")
    status = str(getattr(current, "status", "")).strip().casefold()
    if status in _LAYOUT_STATUSES or status == "archived":
        raise _IntentMismatch("layout and archived sessions cannot receive recovery intent")
    if status not in _RECOVERY_STATUSES:
        raise _IntentMismatch("session status is not recovery eligible")
    values = {field: getattr(current, field, None) for field in _FINGERPRINT_FIELDS}
    if _fingerprint_from_values(values) != request.fingerprint:
        raise _IntentMismatch("managed session changed after the intent was requested")
    try:
        recipe = recipe_from_record(
            current,
            allow_legacy_fallback=False,
            allow_external=False,
        )
    except (SessionRecipeError, ValueError, TypeError) as exc:
        raise _IntentMismatch("managed session recovery recipe is invalid") from exc
    if not automatic_recovery_allowed(recipe):
        raise _IntentMismatch("managed session has no automatic recovery recipe")


def _delete_sidecar(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        _fsync_directory(path.parent)
        return
    except OSError as exc:
        raise RecoveryIntentError("cannot inspect consumed recovery intent") from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != _current_uid()
        or _mode(path_stat) != 0o600
    ):
        raise RecoveryIntentError("consumed recovery intent changed unexpectedly")
    try:
        path.unlink()
    except OSError as exc:
        raise RecoveryIntentError("cannot remove consumed recovery intent") from exc
    _fsync_directory(path.parent)


def _consume_one(store: object, path: Path, request: RecoveryIntentRequest) -> RecoveryIntentOutcome:
    connection = getattr(store, "conn", None)
    getter = getattr(store, "get", None)
    if connection is None or not callable(getter):
        return RecoveryIntentOutcome(request.session_id, "rejected", "managed session store has no SQLite connection")
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        return RecoveryIntentOutcome(request.session_id, "pending", f"managed session database is busy: {exc}")
    try:
        current = getter(request.session_id)
        _validate_current_record(current, request)
        if str(getattr(current, "recovery_intent", "")).strip().casefold() != RECOVERY_INTENT_ACTIVE:
            cursor = connection.execute(
                "UPDATE sessions SET recovery_intent = 'active' WHERE id = ?",
                (request.session_id,),
            )
            if int(cursor.rowcount) != 1:
                raise _IntentMismatch("managed session disappeared before intent update")
            state = "applied"
        else:
            state = "already-active"
        connection.commit()
    except _IntentMismatch as exc:
        if connection.in_transaction:
            connection.rollback()
        return RecoveryIntentOutcome(request.session_id, "rejected", str(exc))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        if connection.in_transaction:
            connection.rollback()
        return RecoveryIntentOutcome(request.session_id, "pending", f"managed session update failed: {exc}")
    try:
        _delete_sidecar(path)
    except RecoveryIntentError as exc:
        return RecoveryIntentOutcome(request.session_id, state, f"{state}; pending file retained: {exc}")
    return RecoveryIntentOutcome(request.session_id, state, "recovery intent applied and pending file consumed")


def _pending_paths(intent_dir: Path) -> list[Path]:
    try:
        with os.scandir(intent_dir) as entries:
            paths = [
                intent_dir / entry.name
                for entry in entries
                if entry.name.endswith(".json")
            ]
    except OSError as exc:
        raise RecoveryIntentError("cannot enumerate recovery intents") from exc
    if len(paths) > _MAX_PENDING_FILES:
        raise RecoveryIntentError("too many pending recovery intents")
    return sorted(paths, key=lambda path: path.name)


def consume_pending_recovery_intents(
    store: object,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[RecoveryIntentOutcome]:
    paths = recovery_intent_paths(environ=environ)
    try:
        intent_stat = paths.intent_dir.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RecoveryIntentError("cannot inspect recovery-intent directory") from exc
    if stat.S_ISLNK(intent_stat.st_mode) or not stat.S_ISDIR(intent_stat.st_mode):
        raise RecoveryIntentError("recovery-intent path is not a real directory")
    _prepare_private_paths(paths)
    expected_database = (paths.state_dir / "sessions.sqlite3").resolve(strict=False)
    try:
        actual_database = Path(getattr(store, "path")).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RecoveryIntentError("managed session store path cannot be verified") from exc
    if actual_database != expected_database:
        raise RecoveryIntentError("recovery intents may only apply to the managed primary database")
    try:
        database_stat = actual_database.lstat()
    except OSError as exc:
        raise RecoveryIntentError("managed primary database cannot be inspected") from exc
    if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
        raise RecoveryIntentError("managed primary database is not a regular file")
    outcomes: list[RecoveryIntentOutcome] = []
    with _intent_lock(paths):
        for path in _pending_paths(paths.intent_dir):
            try:
                request = _read_sidecar(path)
            except RecoveryIntentError as exc:
                session_id = path.stem[:128]
                outcomes.append(
                    RecoveryIntentOutcome(session_id, "rejected", str(exc))
                )
                continue
            outcomes.append(_consume_one(store, path, request))
    return outcomes


__all__ = [
    "RECOVERY_INTENT_FINGERPRINT_SCHEMA",
    "RECOVERY_INTENT_SCHEMA",
    "RecoveryIntentError",
    "RecoveryIntentOutcome",
    "RecoveryIntentPaths",
    "RecoveryIntentRequest",
    "consume_pending_recovery_intents",
    "recovery_intent_paths",
    "request_recovery_intent",
]
