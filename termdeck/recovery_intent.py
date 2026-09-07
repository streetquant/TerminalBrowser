from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
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
RECOVERY_INTENT_FINGERPRINT_SCHEMA = "termdeck.recovery-intent-fingerprint.v2"
_LEGACY_RECOVERY_INTENT_FINGERPRINT_SCHEMA = "termdeck.recovery-intent-fingerprint.v1"
RECOVERY_INTENT_ACK_SCHEMA = "termdeck.recovery-intent-ack.v1"

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 64 * 1024
_MAX_PENDING_FILES = 256
_MAX_TEXT_BYTES = 64 * 1024
_MAX_COMMAND_BYTES = 256 * 1024
_MAX_CWD_BYTES = 16 * 1024
_MAX_LOCK_RETRIES = 4
_MAX_LOCK_WAIT_SECONDS = 0.5
_MAX_DRAIN_BATCHES = 4
_MAX_DIRECTORY_ENTRIES_PER_BATCH = 1024
_DEFAULT_DRAIN_BUDGET_SECONDS = 0.05
_MAX_DRAIN_BUDGET_SECONDS = 1.0
_RETIRED_LOCK_MARKER = b"termdeck.recovery-intent-lock.retired.v1\n"
_DIRECTORY_CURSORS: dict[str, Iterator[os.DirEntry[str]]] = {}
_RECOVERY_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending", "start-error", "stopped"}
)
_LAYOUT_STATUSES = frozenset({"new", "remembered"})
_SOURCE_FIELDS = (
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
_FINGERPRINT_FIELDS = tuple(
    field for field in _SOURCE_FIELDS if field not in {"status", "last_pid"}
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
_ACK_FIELDS = frozenset(set(_SIDECAR_FIELDS) | {"completed_at", "state"})
_REQUIRED_SOURCE_COLUMNS = frozenset(
    set(_SOURCE_FIELDS)
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
    lock_dir: Path
    ack_dir: Path
    temporary_dir: Path
    quarantine_dir: Path


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
        lock_dir=state_dir / "recovery-intent-locks",
        ack_dir=state_dir / "recovery-intent-acks",
        temporary_dir=state_dir / "recovery-intent-tmp",
        quarantine_dir=state_dir / "recovery-intent-quarantine",
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
        except FileExistsError:
            pass
        except OSError as exc:
            raise RecoveryIntentError(f"cannot create private recovery directory: {path}") from exc
        else:
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
    _ensure_private_directory(paths.lock_dir)
    _ensure_private_directory(paths.ack_dir)
    _ensure_private_directory(paths.temporary_dir)
    _ensure_private_directory(paths.quarantine_dir)


class _LockChanged(Exception):
    pass


class _IntentLockBusy(Exception):
    pass


def _lock_path(paths: RecoveryIntentPaths, session_id: str) -> Path:
    return paths.lock_dir / f"{session_id}.lock"


def _ack_path(paths: RecoveryIntentPaths, session_id: str) -> Path:
    return paths.ack_dir / f"{session_id}.json"


def _temporary_path(
    paths: RecoveryIntentPaths,
    session_id: str,
    *,
    kind: str,
) -> Path:
    return paths.temporary_dir / f"{session_id}.{kind}.tmp"


def _validate_lock_stat(path_stat: os.stat_result) -> None:
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != _current_uid()
        or path_stat.st_nlink != 1
        or _mode(path_stat) != 0o600
    ):
        raise RecoveryIntentError("recovery-intent lock is not a private regular file")


def _verify_lock_identity(lock_path: Path, descriptor: int) -> os.stat_result:
    try:
        observed = lock_path.lstat()
        locked = os.fstat(descriptor)
    except FileNotFoundError as exc:
        raise _LockChanged from exc
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent lock cannot be revalidated") from exc
    _validate_lock_stat(locked)
    if (
        observed.st_dev != locked.st_dev
        or observed.st_ino != locked.st_ino
        or observed.st_mode != locked.st_mode
        or observed.st_uid != locked.st_uid
        or observed.st_nlink != locked.st_nlink
    ):
        raise _LockChanged
    _validate_lock_stat(observed)
    return locked


def _lock_state(descriptor: int) -> str:
    try:
        marker = os.pread(descriptor, len(_RETIRED_LOCK_MARKER) + 1, 0)
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent lock state cannot be read") from exc
    if not marker:
        return "active"
    if marker == _RETIRED_LOCK_MARKER:
        return "retired"
    return "poisoned"


def _reset_lock_active(descriptor: int) -> None:
    try:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent lock state cannot be repaired") from exc


def _mark_lock_retired(descriptor: int) -> None:
    try:
        os.ftruncate(descriptor, 0)
        written = os.pwrite(descriptor, _RETIRED_LOCK_MARKER, 0)
        if written != len(_RETIRED_LOCK_MARKER):
            raise RecoveryIntentError("recovery-intent lock state write was incomplete")
        os.fsync(descriptor)
    except RecoveryIntentError:
        raise
    except OSError as exc:
        raise RecoveryIntentError("recovery-intent lock state cannot be retired") from exc


def _sidecar_present(sidecar_path: Path) -> bool:
    try:
        sidecar_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RecoveryIntentError("recovery intent cannot be inspected") from exc
    return True


def _unlink_owned_lock(lock_path: Path, descriptor: int) -> None:
    _verify_lock_identity(lock_path, descriptor)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        raise _LockChanged
    except OSError as exc:
        raise RecoveryIntentError("cannot remove retired recovery-intent lock") from exc
    _fsync_directory(lock_path.parent)


def _open_locked_intent_lock(lock_path: Path, *, wait_seconds: float) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    try:
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
    except FileNotFoundError:
        raise _LockChanged
    except OSError as exc:
        raise RecoveryIntentError("cannot open recovery-intent lock") from exc
    try:
        try:
            path_stat = os.fstat(descriptor)
            _validate_lock_stat(path_stat)
            if created:
                _fsync_directory(lock_path.parent)
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _IntentLockBusy from exc
                    time.sleep(min(0.01, remaining))
            _verify_lock_identity(lock_path, descriptor)
            return descriptor
        except (_IntentLockBusy, _LockChanged):
            raise
        except OSError as exc:
            raise RecoveryIntentError("cannot acquire recovery-intent lock") from exc
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _intent_lock(
    paths: RecoveryIntentPaths,
    session_id: str,
    sidecar_path: Path,
    *,
    wait_seconds: float,
) -> Iterator[None]:
    lock_path = _lock_path(paths, session_id)
    for _ in range(_MAX_LOCK_RETRIES):
        descriptor = -1
        locked = False
        try:
            descriptor = _open_locked_intent_lock(
                lock_path,
                wait_seconds=wait_seconds,
            )
            locked = True
            state = _lock_state(descriptor)
            if state == "poisoned":
                _reset_lock_active(descriptor)
            elif state == "retired":
                if _sidecar_present(sidecar_path):
                    _reset_lock_active(descriptor)
                else:
                    try:
                        _unlink_owned_lock(lock_path, descriptor)
                    except RecoveryIntentError:
                        if lock_path.exists():
                            raise
                    continue
            try:
                yield
            finally:
                if not _sidecar_present(sidecar_path):
                    try:
                        _mark_lock_retired(descriptor)
                        _unlink_owned_lock(lock_path, descriptor)
                    except (_LockChanged, RecoveryIntentError):
                        pass
            return
        except _LockChanged:
            continue
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            if descriptor >= 0:
                os.close(descriptor)
    raise RecoveryIntentError("recovery-intent lock changed repeatedly")


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


def _fingerprint_from_values(
    values: Mapping[str, object],
    *,
    legacy: bool = False,
) -> str:
    fingerprint_fields = _SOURCE_FIELDS if legacy else _FINGERPRINT_FIELDS
    fields = {
        field: _fingerprint_value(field, values.get(field))
        for field in fingerprint_fields
    }
    encoded = _canonical_json(
        {
            "schema": (
                _LEGACY_RECOVERY_INTENT_FINGERPRINT_SCHEMA
                if legacy
                else RECOVERY_INTENT_FINGERPRINT_SCHEMA
            ),
            "fields": fields,
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_matches(values: Mapping[str, object], fingerprint: str) -> bool:
    return fingerprint in {
        _fingerprint_from_values(values),
        _fingerprint_from_values(values, legacy=True),
    }


def _legacy_source_database_path() -> Path:
    candidate = Path(legacy_state_dir()).expanduser()
    source = candidate / "sessions.sqlite3"
    try:
        path_stat = source.lstat()
    except OSError as exc:
        raise RecoveryIntentError("legacy session database is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryIntentError("legacy session database is not a regular file")
    resolved = source.resolve(strict=True)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{resolved}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RecoveryIntentError(
                "legacy session database journal state cannot be inspected"
            ) from exc
        raise RecoveryIntentError(
            "legacy session database is active or not durably checkpointed"
        )
    return resolved


def _source_database_path(paths: RecoveryIntentPaths) -> tuple[Path, str]:
    managed = paths.state_dir / "sessions.sqlite3"
    try:
        managed_stat = managed.lstat()
    except FileNotFoundError:
        return _legacy_source_database_path(), "legacy"
    except OSError as exc:
        raise RecoveryIntentError("managed session database cannot be inspected") from exc
    if (
        stat.S_ISLNK(managed_stat.st_mode)
        or not stat.S_ISREG(managed_stat.st_mode)
        or managed_stat.st_uid != _current_uid()
    ):
        raise RecoveryIntentError("managed session database is not a private regular file")
    try:
        managed_root_stat = paths.managed_root.lstat()
    except OSError as exc:
        raise RecoveryIntentError("managed SSD root cannot be inspected") from exc
    if managed_stat.st_dev != managed_root_stat.st_dev:
        raise RecoveryIntentError("managed session database is outside the attested SSD state")
    return managed.resolve(strict=True), "managed"


def _read_source_row(
    session_id: str,
    *,
    expected_name: str,
    expected_cwd: str,
    paths: RecoveryIntentPaths,
) -> tuple[dict[str, object], str]:
    source, authority = _source_database_path(paths)
    source_uri = f"{source.as_uri()}?mode=ro"
    if authority == "legacy":
        source_uri += "&immutable=1"
    try:
        connection = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=1.0,
        )
    except (OSError, sqlite3.Error) as exc:
        raise RecoveryIntentError(
            f"{authority} session database cannot be opened read-only"
        ) from exc
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
                f"{authority} session database lacks recovery identity fields: "
                + ", ".join(missing)
            )
        before = int(connection.execute("PRAGMA data_version").fetchone()[0])
        selected_fields = [
            field for field in _SOURCE_FIELDS if field in columns
        ]
        selected = ", ".join(selected_fields)
        row = connection.execute(
            f"SELECT {selected} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        if before != after:
            raise RecoveryIntentError(
                f"{authority} session database changed during recovery-intent request"
            )
        if row is None:
            raise RecoveryIntentError(f"{authority} session record was not found")
        values = {
            field: (
                row[field]
                if field in columns
                else _OPTIONAL_SOURCE_DEFAULTS[field]
            )
            for field in _SOURCE_FIELDS
        }
    except RecoveryIntentError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise RecoveryIntentError(
            f"{authority} session record cannot be read safely"
        ) from exc
    finally:
        connection.close()

    actual_id = _validate_session_id(values["id"])
    if actual_id != session_id:
        raise RecoveryIntentError(f"{authority} session identity changed during request")
    name = _bounded_text(values["name"], field="name", maximum=_MAX_TEXT_BYTES)
    cwd = _bounded_text(values["cwd"], field="cwd", maximum=_MAX_CWD_BYTES)
    if name != expected_name or cwd != expected_cwd:
        raise RecoveryIntentError(
            f"{authority} session name or cwd does not match the request"
        )
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


def _ack_payload(request: RecoveryIntentRequest) -> dict[str, object]:
    return {
        "schema": RECOVERY_INTENT_ACK_SCHEMA,
        "intent": RECOVERY_INTENT_ACTIVE,
        "state": "consumed",
        "request_id": request.request_id,
        "created_at": request.created_at,
        "completed_at": utc_now(),
        "session_id": request.session_id,
        "name": request.name,
        "tag": request.tag,
        "cwd": request.cwd,
        "tmux_name": request.tmux_name,
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
            or path_stat.st_nlink != 1
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


def _read_sidecar_if_present(path: Path) -> RecoveryIntentRequest | None:
    try:
        return _read_sidecar(path)
    except RecoveryIntentError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise exc
        raise


def _read_ack(path: Path) -> RecoveryIntentRequest:
    if path.name != f"{path.stem}.json" or _SESSION_ID.fullmatch(path.stem) is None:
        raise RecoveryIntentError("recovery-intent acknowledgement filename is invalid")
    payload = _decode_json_object(_read_file_bounded(path))
    if set(payload) != _ACK_FIELDS:
        raise RecoveryIntentError("recovery-intent acknowledgement fields are invalid")
    if payload.get("schema") != RECOVERY_INTENT_ACK_SCHEMA:
        raise RecoveryIntentError("recovery-intent acknowledgement schema is unsupported")
    if payload.get("intent") != RECOVERY_INTENT_ACTIVE or payload.get("state") != "consumed":
        raise RecoveryIntentError("recovery-intent acknowledgement state is invalid")
    session_id = _validate_session_id(payload.get("session_id"))
    if session_id != path.stem:
        raise RecoveryIntentError(
            "recovery-intent acknowledgement filename does not match its session"
        )
    name = _bounded_text(payload.get("name"), field="name", maximum=_MAX_TEXT_BYTES)
    cwd = _bounded_text(payload.get("cwd"), field="cwd", maximum=_MAX_CWD_BYTES)
    if not Path(cwd).is_absolute():
        raise RecoveryIntentError("recovery-intent acknowledgement cwd must be absolute")
    tag = _bounded_text(payload.get("tag"), field="tag", maximum=_MAX_TEXT_BYTES)
    tmux_name = _bounded_text(
        payload.get("tmux_name"), field="tmux_name", maximum=_MAX_TEXT_BYTES
    )
    created_at = _bounded_text(
        payload.get("created_at"), field="created_at", maximum=_MAX_TEXT_BYTES
    )
    _bounded_text(
        payload.get("completed_at"), field="completed_at", maximum=_MAX_TEXT_BYTES
    )
    request_id = _bounded_text(
        payload.get("request_id"), field="request_id", maximum=64
    )
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise RecoveryIntentError(
            "recovery-intent acknowledgement request_id is invalid"
        ) from exc
    fingerprint = _bounded_text(
        payload.get("fingerprint"), field="fingerprint", maximum=64
    )
    if _SHA256.fullmatch(fingerprint) is None:
        raise RecoveryIntentError(
            "recovery-intent acknowledgement fingerprint is invalid"
        )
    if type(payload.get("external")) is not int or payload.get("external") != 0:
        raise RecoveryIntentError(
            "recovery-intent acknowledgement external marker is invalid"
        )
    return RecoveryIntentRequest(
        session_id=session_id,
        name=name,
        cwd=cwd,
        request_id=request_id,
        created_at=created_at,
        fingerprint=fingerprint,
        queued=False,
        idempotent=True,
        tag=tag,
        tmux_name=tmux_name,
    )


def _read_ack_if_present(path: Path) -> RecoveryIntentRequest | None:
    try:
        return _read_ack(path)
    except RecoveryIntentError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise exc
        raise


def _quarantine_artifact(path: Path, paths: RecoveryIntentPaths) -> bool:
    identity = f"{path.parent.name}/{path.name}"
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:24]
    destination = paths.quarantine_dir / f"artifact-{digest}.rejected"
    try:
        os.replace(path, destination)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RecoveryIntentError("cannot quarantine recovery-intent artifact") from exc
    _fsync_directory(path.parent)
    if destination.parent != path.parent:
        _fsync_directory(destination.parent)
    return True


def _validate_private_artifact(path_stat: os.stat_result) -> None:
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != _current_uid()
        or path_stat.st_nlink != 1
        or _mode(path_stat) != 0o600
    ):
        raise RecoveryIntentError("recovery-intent artifact is not a private regular file")


def _remove_private_artifact(path: Path, paths: RecoveryIntentPaths) -> bool:
    descriptor = -1
    try:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        except OSError:
            return _quarantine_artifact(path, paths)
        locked = os.fstat(descriptor)
        _validate_private_artifact(locked)
        observed = path.lstat()
        if (
            observed.st_dev != locked.st_dev
            or observed.st_ino != locked.st_ino
            or observed.st_mode != locked.st_mode
            or observed.st_uid != locked.st_uid
            or observed.st_nlink != locked.st_nlink
        ):
            raise _LockChanged
        path.unlink()
        _fsync_directory(path.parent)
        return True
    except FileNotFoundError:
        return False
    except (_LockChanged, RecoveryIntentError, OSError):
        return _quarantine_artifact(path, paths)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    temporary_path: Path,
    paths: RecoveryIntentPaths,
) -> None:
    encoded = _canonical_json(dict(payload)).encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise RecoveryIntentError("recovery-intent file is too large")
    temporary_fd = -1
    published = False
    try:
        _remove_private_artifact(temporary_path, paths)
        temporary_fd = os.open(
            temporary_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
        )
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
        if existing is not None:
            try:
                _validate_private_artifact(existing)
            except RecoveryIntentError:
                _quarantine_artifact(path, paths)
        os.replace(temporary_path, path)
        published = True
        _fsync_directory(path.parent)
    except RecoveryIntentError:
        raise
    except OSError as exc:
        raise RecoveryIntentError("cannot durably publish recovery intent") from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if not published:
            try:
                _remove_private_artifact(temporary_path, paths)
            except RecoveryIntentError:
                pass


def _write_sidecar(
    path: Path,
    payload: Mapping[str, object],
    *,
    temporary_path: Path,
    paths: RecoveryIntentPaths,
) -> None:
    _write_private_json(
        path,
        payload,
        temporary_path=temporary_path,
        paths=paths,
    )


def _write_ack(
    path: Path,
    request: RecoveryIntentRequest,
    *,
    temporary_path: Path,
    paths: RecoveryIntentPaths,
) -> None:
    _write_private_json(
        path,
        _ack_payload(request),
        temporary_path=temporary_path,
        paths=paths,
    )


def _request_from_existing(
    existing: RecoveryIntentRequest,
    *,
    session_id: str,
    name: str,
    cwd: str,
    queued: bool | None = None,
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
        queued=existing.queued if queued is None else queued,
        idempotent=True,
        tag=existing.tag,
        tmux_name=existing.tmux_name,
    )


def _managed_ack_is_active(
    paths: RecoveryIntentPaths,
    acknowledgement: RecoveryIntentRequest,
) -> bool:
    database = paths.state_dir / "sessions.sqlite3"
    try:
        database_stat = database.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RecoveryIntentError(
            "managed recovery state cannot be inspected for acknowledgement"
        ) from exc
    if (
        stat.S_ISLNK(database_stat.st_mode)
        or not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != _current_uid()
    ):
        raise RecoveryIntentError(
            "managed recovery state is invalid for acknowledgement"
        )
    try:
        connection = sqlite3.connect(
            f"{database.resolve(strict=True).as_uri()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
    except (OSError, sqlite3.Error) as exc:
        raise RecoveryIntentError(
            "managed recovery state cannot be opened for acknowledgement"
        ) from exc
    try:
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if not _REQUIRED_SOURCE_COLUMNS.issubset(columns):
            raise RecoveryIntentError(
                "managed recovery state lacks acknowledgement identity fields"
            )
        selected_fields = [field for field in _SOURCE_FIELDS if field in columns]
        row = connection.execute(
            f"SELECT recovery_intent, {', '.join(selected_fields)} "
            "FROM sessions WHERE id = ?",
            (acknowledgement.session_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RecoveryIntentError(
            "managed recovery acknowledgement cannot be verified"
        ) from exc
    finally:
        connection.close()
    if row is None:
        return False
    values = {
        field: (
            row[index + 1]
            if field in columns
            else _OPTIONAL_SOURCE_DEFAULTS[field]
        )
        for index, field in enumerate(selected_fields)
    }
    if (
        str(values["id"]) != acknowledgement.session_id
        or str(values["name"]) != acknowledgement.name
        or str(values["cwd"]) != acknowledgement.cwd
    ):
        return False
    if not _fingerprint_matches(values, acknowledgement.fingerprint):
        return False
    return str(row[0] or "").strip().casefold() == RECOVERY_INTENT_ACTIVE


def _acknowledgement_if_active(
    paths: RecoveryIntentPaths,
    acknowledgement_path: Path,
    *,
    session_id: str,
    name: str,
    cwd: str,
) -> RecoveryIntentRequest | None:
    try:
        acknowledgement = _read_ack_if_present(acknowledgement_path)
    except RecoveryIntentError:
        _quarantine_artifact(acknowledgement_path, paths)
        return None
    if acknowledgement is None:
        return None
    if not _managed_ack_is_active(paths, acknowledgement):
        _remove_private_artifact(acknowledgement_path, paths)
        return None
    return _request_from_existing(
        acknowledgement,
        session_id=session_id,
        name=name,
        cwd=cwd,
        queued=False,
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
    acknowledgement_path = _ack_path(paths, session_id)
    try:
        with _intent_lock(
            paths,
            session_id,
            sidecar_path,
            wait_seconds=_MAX_LOCK_WAIT_SECONDS,
        ):
            acknowledgement = _acknowledgement_if_active(
                paths,
                acknowledgement_path,
                session_id=session_id,
                name=name,
                cwd=cwd,
            )
            if acknowledgement is not None:
                return acknowledgement
            try:
                existing = _read_sidecar_if_present(sidecar_path)
            except RecoveryIntentError:
                _quarantine_artifact(sidecar_path, paths)
                existing = None
            if existing is not None:
                return _request_from_existing(
                    existing,
                    session_id=session_id,
                    name=name,
                    cwd=cwd,
                )
    except _IntentLockBusy as exc:
        raise RecoveryIntentError(
            "recovery-intent request is already changing for this session"
        ) from exc
    _read_source_row(
        session_id,
        expected_name=name,
        expected_cwd=cwd,
        paths=paths,
    )
    try:
        with _intent_lock(
            paths,
            session_id,
            sidecar_path,
            wait_seconds=_MAX_LOCK_WAIT_SECONDS,
        ):
            acknowledgement = _acknowledgement_if_active(
                paths,
                acknowledgement_path,
                session_id=session_id,
                name=name,
                cwd=cwd,
            )
            if acknowledgement is not None:
                return acknowledgement
            try:
                existing = _read_sidecar_if_present(sidecar_path)
            except RecoveryIntentError:
                _quarantine_artifact(sidecar_path, paths)
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
                paths=paths,
            )
            request = RecoveryIntentRequest(
                session_id=session_id,
                name=name,
                cwd=cwd,
                request_id=str(uuid.uuid4()),
                created_at=utc_now(),
                fingerprint=fingerprint,
                tag=_bounded_text(
                    values["tag"], field="tag", maximum=_MAX_TEXT_BYTES
                ),
                tmux_name=_bounded_text(
                    values["tmux_name"],
                    field="tmux_name",
                    maximum=_MAX_TEXT_BYTES,
                ),
            )
            _write_sidecar(
                sidecar_path,
                _sidecar_payload(request, values),
                temporary_path=_temporary_path(paths, session_id, kind="intent"),
                paths=paths,
            )
            return request
    except _IntentLockBusy as exc:
        raise RecoveryIntentError(
            "recovery-intent request is already changing for this session"
        ) from exc


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
    values = {field: getattr(current, field, None) for field in _SOURCE_FIELDS}
    if not _fingerprint_matches(values, request.fingerprint):
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


def _consume_one(
    store: object,
    path: Path,
    request: RecoveryIntentRequest,
    paths: RecoveryIntentPaths,
) -> RecoveryIntentOutcome:
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
        already_active = (
            str(getattr(current, "recovery_intent", "")).strip().casefold()
            == RECOVERY_INTENT_ACTIVE
        )
        cursor = connection.execute(
            """
            UPDATE sessions
            SET recovery_intent = 'active',
                status = CASE
                    WHEN lower(trim(status)) = 'stopped' THEN 'recovery-pending'
                    ELSE status
                END,
                visible = 1
            WHERE id = ?
            """,
            (request.session_id,),
        )
        if int(cursor.rowcount) != 1:
            raise _IntentMismatch("managed session disappeared before intent update")
        state = "already-active" if already_active else "applied"
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
        _write_ack(
            _ack_path(paths, request.session_id),
            request,
            temporary_path=_temporary_path(paths, request.session_id, kind="ack"),
            paths=paths,
        )
    except RecoveryIntentError as exc:
        return RecoveryIntentOutcome(
            request.session_id,
            "pending",
            f"{state}; terminal acknowledgement pending: {exc}",
        )
    try:
        _remove_private_artifact(path, paths)
    except RecoveryIntentError as exc:
        return RecoveryIntentOutcome(
            request.session_id,
            "pending",
            f"{state}; terminal acknowledgement recorded; pending file retained: {exc}",
        )
    return RecoveryIntentOutcome(
        request.session_id,
        state,
        "recovery intent applied, acknowledged, and pending file consumed",
    )


def _pending_batch(
    intent_dir: Path,
    *,
    excluded_names: frozenset[str],
) -> tuple[list[Path], list[Path], bool]:
    pending: list[Path] = []
    residue: list[Path] = []
    entries, exhausted = _bounded_directory_entries(
        intent_dir,
        limit=_MAX_PENDING_FILES,
    )
    for path in entries:
        name = path.name
        if name in excluded_names:
            continue
        if (
            name.endswith(".json")
            and not name.startswith(".")
            and _SESSION_ID.fullmatch(name[:-5]) is not None
        ):
            pending.append(path)
        else:
            residue.append(path)
    return (
        sorted(pending, key=lambda path: path.name),
        residue,
        not exhausted,
    )


def recovery_intent_backlog_pending(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    paths = recovery_intent_paths(environ=environ)
    for directory in (paths.intent_dir, paths.lock_dir, paths.temporary_dir):
        try:
            directory_stat = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RecoveryIntentError("cannot inspect recovery-intent directory") from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise RecoveryIntentError("recovery-intent path is not a real directory")
        try:
            with os.scandir(directory) as entries:
                for _ in entries:
                    return True
        except OSError as exc:
            raise RecoveryIntentError("cannot enumerate recovery intents") from exc
        _reset_directory_cursor(directory)
    return False


def _reset_directory_cursor(directory: Path) -> None:
    iterator = _DIRECTORY_CURSORS.pop(os.fspath(directory), None)
    if iterator is not None:
        try:
            iterator.close()
        except Exception:
            pass


def _bounded_directory_entries(
    directory: Path,
    *,
    limit: int = _MAX_DIRECTORY_ENTRIES_PER_BATCH,
) -> tuple[list[Path], bool]:
    key = os.fspath(directory)
    for attempt in range(2):
        iterator = _DIRECTORY_CURSORS.get(key)
        if iterator is None:
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise RecoveryIntentError(
                    "cannot enumerate recovery-intent artifacts"
                ) from exc
            _DIRECTORY_CURSORS[key] = iterator
        result: list[Path] = []
        exhausted = False
        try:
            for _ in range(max(1, int(limit))):
                try:
                    entry = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                result.append(directory / entry.name)
        except (OSError, RuntimeError) as exc:
            _reset_directory_cursor(directory)
            if attempt == 0:
                continue
            raise RecoveryIntentError(
                "cannot enumerate recovery-intent artifacts"
            ) from exc
        if exhausted:
            _reset_directory_cursor(directory)
        if result or attempt:
            return result, exhausted
    return [], True


def _temporary_session_id(name: str) -> str | None:
    for kind in ("intent", "ack"):
        suffix = f".{kind}.tmp"
        if name.endswith(suffix):
            session_id = name[: -len(suffix)]
            if _SESSION_ID.fullmatch(session_id) is not None:
                return session_id
    return None


def _cleanup_orphan_artifacts(
    paths: RecoveryIntentPaths,
    *,
    deadline: float,
) -> None:
    temporary_paths, _temporary_exhausted = _bounded_directory_entries(
        paths.temporary_dir
    )
    for temporary_path in temporary_paths:
        if time.monotonic() >= deadline:
            return
        session_id = _temporary_session_id(temporary_path.name)
        if session_id is None:
            try:
                _quarantine_artifact(temporary_path, paths)
            except RecoveryIntentError:
                pass
            continue
        sidecar_path = paths.intent_dir / f"{session_id}.json"
        try:
            with _intent_lock(
                paths,
                session_id,
                sidecar_path,
                wait_seconds=0.0,
            ):
                _remove_private_artifact(temporary_path, paths)
        except _IntentLockBusy:
            continue
        except RecoveryIntentError:
            try:
                _quarantine_artifact(temporary_path, paths)
            except RecoveryIntentError:
                pass
    lock_paths, _locks_exhausted = _bounded_directory_entries(paths.lock_dir)
    for lock_path in lock_paths:
        if time.monotonic() >= deadline:
            return
        if not lock_path.name.endswith(".lock"):
            try:
                _quarantine_artifact(lock_path, paths)
            except RecoveryIntentError:
                pass
            continue
        session_id = lock_path.name[:-5]
        if _SESSION_ID.fullmatch(session_id) is None:
            try:
                _quarantine_artifact(lock_path, paths)
            except RecoveryIntentError:
                pass
            continue
        sidecar_path = paths.intent_dir / f"{session_id}.json"
        if _sidecar_present(sidecar_path):
            continue
        try:
            with _intent_lock(
                paths,
                session_id,
                sidecar_path,
                wait_seconds=0.0,
            ):
                pass
        except _IntentLockBusy:
            continue
        except RecoveryIntentError:
            try:
                _quarantine_artifact(lock_path, paths)
            except RecoveryIntentError:
                pass


def consume_pending_recovery_intents(
    store: object,
    *,
    environ: Mapping[str, str] | None = None,
    time_budget_seconds: float = _DEFAULT_DRAIN_BUDGET_SECONDS,
) -> list[RecoveryIntentOutcome]:
    try:
        budget = float(time_budget_seconds)
    except (TypeError, ValueError) as exc:
        raise RecoveryIntentError("recovery-intent drain budget is invalid") from exc
    if budget <= 0.0 or budget != budget or budget > _MAX_DRAIN_BUDGET_SECONDS:
        raise RecoveryIntentError("recovery-intent drain budget is outside safe bounds")
    deadline = time.monotonic() + budget
    paths = recovery_intent_paths(environ=environ)
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
    attempted_names: set[str] = set()
    for _ in range(_MAX_DRAIN_BATCHES):
        if time.monotonic() >= deadline:
            break
        pending, residue, saturated = _pending_batch(
            paths.intent_dir,
            excluded_names=frozenset(attempted_names),
        )
        for artifact in residue:
            if time.monotonic() >= deadline:
                break
            try:
                _quarantine_artifact(artifact, paths)
            except RecoveryIntentError:
                continue
        if not pending:
            break
        made_progress = False
        for path in pending:
            if time.monotonic() >= deadline:
                break
            attempted_names.add(path.name)
            session_id = path.stem
            try:
                with _intent_lock(
                    paths,
                    session_id,
                    path,
                    wait_seconds=0.0,
                ):
                    try:
                        request = _read_sidecar_if_present(path)
                    except RecoveryIntentError as exc:
                        _quarantine_artifact(path, paths)
                        made_progress = True
                        outcomes.append(
                            RecoveryIntentOutcome(session_id, "rejected", str(exc))
                        )
                        continue
                    if request is None:
                        made_progress = True
                        continue
                    outcome = _consume_one(store, path, request, paths)
                    if outcome.state == "rejected":
                        try:
                            _quarantine_artifact(path, paths)
                            made_progress = True
                        except RecoveryIntentError as exc:
                            outcome = RecoveryIntentOutcome(
                                outcome.session_id,
                                "pending",
                                f"{outcome.detail}; quarantine failed: {exc}",
                            )
                    elif not _sidecar_present(path):
                        made_progress = True
                    outcomes.append(outcome)
            except _IntentLockBusy:
                outcomes.append(
                    RecoveryIntentOutcome(
                        session_id,
                        "pending",
                        "recovery-intent session is busy; deferred without blocking startup",
                    )
                )
            except RecoveryIntentError as exc:
                outcomes.append(
                    RecoveryIntentOutcome(
                        session_id,
                        "pending",
                        f"recovery-intent session deferred safely: {exc}",
                    )
                )
        if time.monotonic() >= deadline or not saturated or not made_progress:
            break
    if time.monotonic() < deadline:
        _cleanup_orphan_artifacts(paths, deadline=deadline)
    return outcomes


__all__ = [
    "RECOVERY_INTENT_ACK_SCHEMA",
    "RECOVERY_INTENT_FINGERPRINT_SCHEMA",
    "RECOVERY_INTENT_SCHEMA",
    "RecoveryIntentError",
    "RecoveryIntentOutcome",
    "RecoveryIntentPaths",
    "RecoveryIntentRequest",
    "consume_pending_recovery_intents",
    "recovery_intent_backlog_pending",
    "recovery_intent_paths",
    "request_recovery_intent",
]
