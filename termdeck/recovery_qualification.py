"""Read-only qualification of remembered panes for managed recovery.

Qualification is deliberately separate from recovery execution.  It opens the
session registry in SQLite read-only mode, inspects only bounded rollout
metadata, and reports a decision for every row.  It never creates a claim,
starts tmux, or invokes Codex.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping

from .session_recipe import SessionRecipe, SessionRecipeError, recipe_from_record
from .session_store import default_db_path
from .sqlite_io import SQLiteReadOnlyError, read_only_refresh


CLASSIFICATIONS = ("eligible", "deferred", "fail_closed", "skipped")
_CLASSIFICATION_SET = frozenset(CLASSIFICATIONS)
_ACTIVE_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending", "start-error", "stopped"}
)
_LAYOUT_STATUSES = frozenset({"new", "remembered"})
_INACTIVE_INTENTS = frozenset(
    {"inactive", "archived", "manual", "never", "stopped", "terminated", "false", "no", "0"}
)
_ACTIVE_INTENTS = frozenset(
    {"active", "eligible", "recover", "recovery", "resume", "running", "true", "yes", "1"}
)
_CODEX_BASENAMES = frozenset({"codex", "codex-contained-user"})
_OTHER_CODEX_BASENAMES = frozenset({"codex-opencode", "opencode"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_ROLLOUT_FILES = 4096
_MAX_METADATA_BYTES = 65_536
_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "name",
        "command",
        "cwd",
        "status",
    }
)
_OPTIONAL_DEFAULTS: dict[str, object] = {
    "tag": "",
    "tmux_name": "",
    "visible": 1,
    "external": 0,
    "pane_target": None,
    "recovery_command": None,
    "recovery_cwd": None,
    "auth_scope_id": None,
    "codex_home": None,
    "recovery_recipe": None,
    "recovery_recipe_sha256": None,
    "restart_policy": "on-failure",
    "recovery_intent": "legacy",
    "recovery_attempts": 0,
}
_HARD_FAIL_CODES = frozenset(
    {
        "session_id_missing",
        "session_id_invalid",
        "status_invalid",
        "recipe_missing",
        "cwd_unqualified",
        "cwd_invalid",
        "cwd_mismatch",
        "auth_scope_missing",
        "auth_scope_invalid",
        "codex_rollout_not_exact",
        "unsupported_tool",
        "coordinator_target_missing",
        "coordinator_claim_table_invalid",
        "coordinator_claim_unreadable",
        "coordinator_claim_invalid",
    }
)


@dataclass(frozen=True)
class QualificationReason:
    """One exact reason contributing to a pane's decision."""

    code: str
    detail: str
    gate: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "gate": self.gate}


@dataclass(frozen=True)
class QualificationCheck:
    """A required gate's observed state."""

    state: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class QualificationRow:
    """The single classification assigned to one database row."""

    row_number: int
    session_id: str
    name: str
    status: str
    classification: str
    primary_reason: QualificationReason
    reasons: tuple[QualificationReason, ...] = ()
    checks: Mapping[str, QualificationCheck] = field(default_factory=dict)
    queued_intent: Mapping[str, object] | None = None
    target_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.classification not in _CLASSIFICATION_SET:
            raise ValueError(f"unknown recovery qualification: {self.classification}")

    def as_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "id": self.session_id,
            "name": self.name,
            "status": self.status,
            "classification": self.classification,
            "primary_reason": self.primary_reason.as_dict(),
            "reasons": [reason.as_dict() for reason in self.reasons],
            "checks": {
                name: check.as_dict()
                for name, check in sorted(self.checks.items())
            },
            "queued_intent": dict(self.queued_intent) if self.queued_intent else None,
            "target_keys": list(self.target_keys),
        }


@dataclass(frozen=True)
class RecoveryQualificationReport:
    """Complete, disjoint read-only qualification output."""

    db_path: str
    panes: tuple[QualificationRow, ...]
    read_only: bool = True
    ok: bool = True
    error: str | None = None
    source_data_version: int | None = None
    source_data_version_after: int | None = None

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(row.classification for row in self.panes)
        return {classification: int(counter.get(classification, 0)) for classification in CLASSIFICATIONS}

    def as_dict(self) -> dict[str, object]:
        counts = self.counts
        count = len(self.panes)
        accounted = sum(counts.values())
        return {
            "action": "recovery-qualification",
            "ok": self.ok,
            "read_only": self.read_only,
            "mutated": False,
            "db": self.db_path,
            "count": count,
            "classification_counts": counts,
            "accounting": {
                "input": count,
                "classified": accounted,
                "unaccounted": count - accounted,
                "disjoint": accounted == count,
            },
            "source_data_version": self.source_data_version,
            "source_data_version_after": self.source_data_version_after,
            "error": self.error,
            "panes": [row.as_dict() for row in self.panes],
        }


@dataclass(frozen=True)
class _RolloutObservation:
    state: str
    detail: str


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalise_intent(value: object) -> str:
    raw = _text(value).casefold()
    if raw in _ACTIVE_INTENTS:
        return "active"
    if raw in _INACTIVE_INTENTS:
        return "inactive"
    return "legacy"


def _is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return _text(value).casefold() in _ACTIVE_INTENTS


def _reason(code: str, detail: str, gate: str) -> QualificationReason:
    return QualificationReason(code=code, detail=detail, gate=gate)


def _check(state: str, detail: str) -> QualificationCheck:
    return QualificationCheck(state=state, detail=detail)


def _failure_classification(reasons: Iterable[QualificationReason]) -> str:
    reason_list = tuple(reasons)
    if not reason_list:
        return "eligible"
    if any(
        reason.code in {"external_session", "archived_session", "layout_only", "inactive_intent"}
        for reason in reason_list
    ):
        return "skipped"
    if any(
        reason.code in _HARD_FAIL_CODES
        or reason.code.endswith("_invalid")
        or reason.code.endswith("_malformed")
        for reason in reason_list
    ):
        return "fail_closed"
    return "deferred"


def _absolute_path(value: object) -> Path | None:
    raw = _text(value)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    return path


def _path_identity(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _is_codex_command(command: object) -> tuple[bool, bool]:
    try:
        tokens = shlex.split(_text(command))
    except ValueError:
        return False, False
    native = False
    other = False
    for token in tokens:
        basename = Path(token).name.casefold()
        native = native or basename in _CODEX_BASENAMES
        other = other or basename in _OTHER_CODEX_BASENAMES
    return native, other


def _resume_uuid(command: object) -> str | None:
    try:
        tokens = shlex.split(_text(command))
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        candidate: str | None = None
        if token == "resume" and index + 1 < len(tokens):
            candidate = tokens[index + 1]
        elif token == "--resume" and index + 1 < len(tokens):
            candidate = tokens[index + 1]
        elif token.startswith("--resume="):
            candidate = token.partition("=")[2]
        if candidate:
            try:
                return str(uuid.UUID(candidate))
            except (ValueError, AttributeError, TypeError):
                return None
    return None


def _target_key(kind: str, value: object) -> str | None:
    normalized = _text(value)
    if not normalized:
        return None
    digest = hashlib.sha256(
        f"{kind}\0{normalized}".encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"{kind}:{digest}"


def _target_keys(record: object, recipe: SessionRecipe | None) -> tuple[str, ...]:
    values: list[tuple[str, object]] = [
        ("session", getattr(record, "id", "")),
        ("tmux", getattr(record, "tmux_name", "")),
        ("pane", getattr(record, "pane_target", "")),
    ]
    native, _ = _is_codex_command(
        getattr(record, "recovery_command", None)
        or (recipe.command if recipe is not None else getattr(record, "command", ""))
    )
    if native:
        auth_scope = _text(getattr(record, "auth_scope_id", ""))
        command = _text(getattr(record, "recovery_command", ""))
        if not command and recipe is not None:
            command = recipe.command
        if not command:
            command = _text(getattr(record, "command", ""))
        if resume_id := _resume_uuid(command):
            values.append(
                (
                    "auth-resume" if auth_scope else "resume",
                    f"{auth_scope}\0{resume_id}" if auth_scope else resume_id,
                )
            )
    keys = {
        key
        for kind, value in values
        if (key := _target_key(kind, value)) is not None
    }
    return tuple(sorted(keys))


def _queued_intent(record: object, cwd: str) -> dict[str, object] | None:
    session_id = _text(getattr(record, "id", ""))
    name = _text(getattr(record, "name", ""))
    if not session_id or not name or not cwd:
        return None
    return {
        "schema": "termdeck.recovery-intent.v1",
        "intent": "active",
        "queued": True,
        "read_only": True,
        "session_id": session_id,
        "name": name,
        "cwd": cwd,
    }


def _record_from_row(row: Mapping[str, object]) -> SimpleNamespace:
    values = dict(_OPTIONAL_DEFAULTS)
    values.update({key: row[key] for key in row.keys()})
    return SimpleNamespace(**values)


def _open_read_only(path: Path) -> sqlite3.Connection:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise OSError(f"session database is unavailable: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise OSError(f"session database is not a regular file: {path}")
    connection = sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=0.5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if not query_only or int(query_only[0]) != 1:
        connection.close()
        raise OSError("session database did not enter query-only mode")
    return connection


def _schema_columns(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("PRAGMA table_info(sessions)").fetchall()
    columns = {str(row[1]) for row in rows}
    if not columns:
        raise OSError("session database has no sessions table")
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise OSError("session database lacks required columns: " + ", ".join(missing))
    return columns


def _rollout_from_index(
    session_id: str,
    *,
    rollout_states: Mapping[str, object] | None,
) -> _RolloutObservation | None:
    if rollout_states is None or session_id not in rollout_states:
        return None
    value = rollout_states[session_id]
    if isinstance(value, bool):
        return _RolloutObservation(
            "exact" if value else "missing",
            "rollout state supplied by caller",
        )
    if isinstance(value, Mapping):
        state = _text(value.get("state") or value.get("status")).casefold()
        detail = _text(value.get("detail")) or "rollout state supplied by caller"
    else:
        state = _text(value).casefold()
        detail = "rollout state supplied by caller"
    aliases = {
        "present": "exact",
        "ready": "exact",
        "valid": "exact",
        "missing": "missing",
        "absent": "missing",
        "ambiguous": "ambiguous",
        "invalid": "invalid",
        "uncertain": "uncertain",
    }
    return _RolloutObservation(aliases.get(state, state or "uncertain"), detail)


def _read_rollout_metadata(path: Path, session_id: str, cwd: str) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            line = handle.readline(_MAX_METADATA_BYTES + 1)
        if not line or len(line.encode("utf-8")) > _MAX_METADATA_BYTES:
            return False
        payload = json.loads(line)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    body = payload.get("payload", payload)
    if not isinstance(body, Mapping) or body.get("source") != "cli":
        return False
    raw_id = body.get("id") or body.get("session_id")
    try:
        observed_id = str(uuid.UUID(str(raw_id)))
    except (ValueError, TypeError, AttributeError):
        return False
    observed_cwd = _absolute_path(body.get("cwd"))
    target_cwd = _absolute_path(cwd)
    if observed_id != session_id or observed_cwd is None or target_cwd is None:
        return False
    observed_identity = _path_identity(observed_cwd)
    target_identity = _path_identity(target_cwd)
    return observed_identity is not None and target_identity is not None and observed_identity == target_identity


def _rollout_observation(
    session_id: str,
    cwd: str,
    codex_home: str,
    *,
    rollout_root: str | Path | None,
    rollout_states: Mapping[str, object] | None,
    max_rollout_files: int,
) -> _RolloutObservation:
    indexed = _rollout_from_index(session_id, rollout_states=rollout_states)
    if indexed is not None:
        return indexed
    if rollout_root is None and _absolute_path(codex_home) is None:
        return _RolloutObservation("missing", "native Codex auth scope has no exact rollout root")
    root = Path(rollout_root).expanduser() if rollout_root is not None else Path(codex_home).expanduser() / "sessions"
    if not root.is_dir():
        return _RolloutObservation("missing", f"Codex rollout root is unavailable: {root}")
    try:
        paths = list(root.rglob("*.jsonl"))
    except (OSError, RuntimeError) as exc:
        return _RolloutObservation("uncertain", f"Codex rollout root cannot be inspected: {exc}")
    if len(paths) > max(1, min(int(max_rollout_files), _MAX_ROLLOUT_FILES)):
        return _RolloutObservation("uncertain", "Codex rollout scan exceeded the bounded audit limit")
    matches = [path for path in paths if path.is_file() and _read_rollout_metadata(path, session_id, cwd)]
    if len(matches) == 1:
        return _RolloutObservation("exact", f"exact CLI rollout metadata found at {matches[0]}")
    if len(matches) > 1:
        return _RolloutObservation("ambiguous", "multiple exact CLI rollout files matched the target UUID")
    return _RolloutObservation("missing", "no exact CLI rollout metadata matched the target UUID and cwd")


def _storage_observation(environment: Mapping[str, str]) -> tuple[str, QualificationReason | None, QualificationCheck]:
    # Direct-root evidence does not infer bulk-storage masking.
    expected = {
        "CODEX_STORAGE_FABRIC": "direct-root",
        "CODEX_WORKSPACE_STORAGE_CLASS": "non-rotational-block",
    }
    for key, expected_value in expected.items():
        actual = _text(environment.get(key))
        if not actual:
            return "deferred", _reason("storage_attestation_missing", f"{key} is not present", "storage_attestation"), _check("deferred", f"{key} is missing")
        if actual != expected_value:
            return "fail_closed", _reason("storage_attestation_invalid", f"{key}={actual!r}; expected {expected_value!r}", "storage_attestation"), _check("invalid", f"{key} does not match the direct-root contract")
    attestation = _text(environment.get("CODEX_STORAGE_ATTESTATION"))
    if not attestation or attestation.casefold() == "none":
        return "deferred", _reason("storage_attestation_missing", "CODEX_STORAGE_ATTESTATION is absent", "storage_attestation"), _check("deferred", "workspace receipt is missing")
    return "pass", None, _check("pass", "direct-root non-rotational block storage and receipt are present")


def _auth_observation(record: object, *, native_codex: bool) -> tuple[str, QualificationReason | None, QualificationCheck]:
    if not native_codex:
        return "not_applicable", None, _check("not_applicable", "session is not a native Codex recovery")
    scope_id = _text(getattr(record, "auth_scope_id", ""))
    home_raw = _text(getattr(record, "codex_home", ""))
    if not scope_id or not home_raw:
        return "fail_closed", _reason("auth_scope_missing", "native Codex recovery lacks an exact auth_scope_id and codex_home", "auth_scope"), _check("invalid", "auth scope identity is incomplete")
    if _SCOPE_ID.fullmatch(scope_id) is None:
        return "fail_closed", _reason("auth_scope_invalid", "auth_scope_id is not a safe exact scope identifier", "auth_scope"), _check("invalid", "auth scope identifier is malformed")
    home = _absolute_path(home_raw)
    if home is None:
        return "fail_closed", _reason("auth_scope_invalid", "codex_home must be absolute", "auth_scope"), _check("invalid", "codex_home is not absolute")
    try:
        path_stat = home.lstat()
    except FileNotFoundError:
        return "deferred", _reason("auth_scope_unavailable", f"exact codex_home is not present: {home}", "auth_scope"), _check("deferred", "auth scope directory is unavailable")
    except OSError as exc:
        return "deferred", _reason("auth_scope_unavailable", f"exact codex_home cannot be inspected: {exc}", "auth_scope"), _check("deferred", "auth scope directory cannot be inspected")
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        return "fail_closed", _reason("auth_scope_invalid", f"codex_home is not a real directory: {home}", "auth_scope"), _check("invalid", "auth scope is not a real directory")
    return "pass", None, _check("pass", f"exact auth scope {scope_id} maps to {home}")


def _cwd_observation(record: object, recipe: SessionRecipe | None) -> tuple[str, str, QualificationReason | None, QualificationCheck]:
    raw_record_cwd = _text(getattr(record, "cwd", ""))
    record_cwd = _absolute_path(raw_record_cwd)
    if record_cwd is None:
        return "fail_closed", raw_record_cwd, _reason("cwd_invalid", "remembered cwd is missing or not absolute", "cwd"), _check("invalid", "remembered cwd is not an exact absolute path")
    if recipe is None:
        return "fail_closed", raw_record_cwd, _reason("cwd_unqualified", "an exact recipe is required before cwd qualification", "cwd"), _check("invalid", "recipe did not provide an exact cwd")
    recipe_cwd = _absolute_path(recipe.cwd)
    if recipe_cwd is None:
        return "fail_closed", recipe.cwd, _reason("cwd_invalid", "recipe cwd is missing or not absolute", "cwd"), _check("invalid", "recipe cwd is not absolute")
    try:
        record_identity = _path_identity(record_cwd)
        recipe_identity = _path_identity(recipe_cwd)
    except (OSError, RuntimeError):
        record_identity = recipe_identity = None
    if record_identity is None or recipe_identity is None:
        missing = recipe_cwd if recipe_identity is None else record_cwd
        return "deferred", str(recipe_cwd), _reason("cwd_unavailable", f"exact working directory is unavailable: {missing}", "cwd"), _check("deferred", "exact working directory cannot be resolved")
    if record_identity != recipe_identity:
        return "fail_closed", str(recipe_cwd), _reason("cwd_mismatch", f"recipe cwd {recipe_cwd} does not identify the remembered cwd {record_cwd}", "cwd"), _check("invalid", "recipe and remembered cwd identify different directories")
    return "pass", str(recipe_cwd), None, _check("pass", f"exact cwd {recipe_cwd} exists and matches the remembered pane")


def _recipe_observation(record: object) -> tuple[str, SessionRecipe | None, QualificationReason | None, QualificationCheck]:
    encoded = getattr(record, "recovery_recipe", None)
    digest = getattr(record, "recovery_recipe_sha256", None)
    if encoded is None or digest is None:
        return "fail_closed", None, _reason("recipe_missing", "canonical recovery_recipe and recovery_recipe_sha256 are required", "recipe"), _check("invalid", "canonical recipe receipt is absent")
    try:
        recipe = recipe_from_record(record, allow_legacy_fallback=False, allow_external=False)
    except (SessionRecipeError, TypeError, ValueError) as exc:
        return "fail_closed", None, _reason("recipe_invalid", f"canonical recovery recipe is invalid: {exc}", "recipe"), _check("invalid", "recipe digest or schema validation failed")
    if recipe is None:
        return "fail_closed", None, _reason("recipe_invalid", "canonical recovery recipe could not be decoded", "recipe"), _check("invalid", "recipe is unavailable")
    if recipe.restart_policy not in {"always", "on-failure"}:
        return "skipped", recipe, _reason("restart_policy_inactive", f"restart policy {recipe.restart_policy!r} does not authorize automatic recovery", "recipe"), _check("not_applicable", f"restart policy is {recipe.restart_policy}")
    return "pass", recipe, None, _check("pass", f"canonical recipe {recipe.schema} with {recipe.restart_policy} policy")


def _claim_observation(
    connection: sqlite3.Connection,
    target_keys: tuple[str, ...],
    *,
    now: float,
) -> tuple[str, QualificationReason | None, QualificationCheck]:
    if not target_keys:
        return "fail_closed", _reason("coordinator_target_missing", "no durable single-flight target key could be derived", "coordinator_claimability"), _check("invalid", "recovery target has no durable key")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'recovery_claims'"
        ).fetchall()
    }
    if not tables:
        return "deferred", _reason("coordinator_claim_table_missing", "durable recovery_claims table has not been initialized", "coordinator_claimability"), _check("deferred", "claimability cannot be proven without the durable table")
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(recovery_claims)").fetchall()}
    except sqlite3.Error as exc:
        return "fail_closed", _reason("coordinator_claim_table_invalid", f"durable claim table cannot be inspected: {exc}", "coordinator_claimability"), _check("invalid", "claim table schema is unreadable")
    required = {"target_key", "session_id", "owner_id", "claim_token", "expires_at"}
    if not required.issubset(columns):
        return "fail_closed", _reason("coordinator_claim_table_invalid", "durable claim table lacks required columns", "coordinator_claimability"), _check("invalid", "claim table schema is incomplete")
    placeholders = ",".join("?" for _ in target_keys)
    try:
        rows = connection.execute(
            f"SELECT target_key, session_id, expires_at FROM recovery_claims WHERE target_key IN ({placeholders})",
            target_keys,
        ).fetchall()
    except sqlite3.Error as exc:
        return "fail_closed", _reason("coordinator_claim_unreadable", f"durable claim rows cannot be inspected: {exc}", "coordinator_claimability"), _check("invalid", "claim rows are unreadable")
    for row in rows:
        try:
            expires_at = float(row[2])
        except (TypeError, ValueError):
            return "fail_closed", _reason("coordinator_claim_invalid", "durable claim expiry is not numeric", "coordinator_claimability"), _check("invalid", "claim expiry is malformed")
        if expires_at > now:
            return "deferred", _reason("coordinator_claim_held", f"target {row[0]} is claimed by session {row[1]}", "coordinator_claimability"), _check("deferred", "an unexpired durable recovery claim exists")
    return "pass", None, _check("pass", "all durable recovery target keys are claimable at audit time")


def _rollout_gate(
    record: object,
    recipe: SessionRecipe | None,
    cwd: str,
    *,
    rollout_root: str | Path | None,
    rollout_states: Mapping[str, object] | None,
    max_rollout_files: int,
) -> tuple[str, QualificationReason | None, QualificationCheck]:
    native, other = _is_codex_command(
        getattr(record, "recovery_command", None)
        or (recipe.command if recipe is not None else getattr(record, "command", ""))
    )
    if other:
        return "fail_closed", _reason("unsupported_tool", "recovery command names an unmanaged Codex-compatible tool", "codex_rollout"), _check("invalid", "managed native Codex rollout proof is unavailable")
    if not native:
        return "not_applicable", None, _check("not_applicable", "session is not a native Codex recovery")
    command = getattr(record, "recovery_command", None) or (recipe.command if recipe is not None else getattr(record, "command", ""))
    session_id = _resume_uuid(command)
    if session_id is None:
        return "fail_closed", _reason("codex_rollout_not_exact", "native Codex recipe does not carry one canonical resume UUID", "codex_rollout"), _check("invalid", "exact Codex rollout identity is absent")
    observation = _rollout_observation(
        session_id,
        cwd,
        _text(getattr(record, "codex_home", "")),
        rollout_root=rollout_root,
        rollout_states=rollout_states,
        max_rollout_files=max_rollout_files,
    )
    if observation.state == "exact":
        return "pass", None, _check("pass", observation.detail)
    if observation.state == "invalid":
        return "fail_closed", _reason("codex_rollout_invalid", observation.detail, "codex_rollout"), _check("invalid", observation.detail)
    if observation.state == "ambiguous":
        return "fail_closed", _reason("codex_rollout_ambiguous", observation.detail, "codex_rollout"), _check("invalid", observation.detail)
    if observation.state == "uncertain":
        return "deferred", _reason("codex_rollout_uncertain", observation.detail, "codex_rollout"), _check("deferred", observation.detail)
    return "deferred", _reason("codex_rollout_missing", observation.detail, "codex_rollout"), _check("deferred", observation.detail)


def _qualify_row(
    row_number: int,
    record: object,
    connection: sqlite3.Connection,
    *,
    environment: Mapping[str, str],
    rollout_root: str | Path | None,
    rollout_states: Mapping[str, object] | None,
    max_rollout_files: int,
    now: float,
) -> QualificationRow:
    raw_session_id = _text(getattr(record, "id", ""))
    session_id = raw_session_id or f"row-{row_number}"
    name = _text(getattr(record, "name", "")) or "(unnamed pane)"
    status = _text(getattr(record, "status", "")).casefold() or "(missing)"
    checks: dict[str, QualificationCheck] = {}
    reasons: list[QualificationReason] = []
    native_codex, other_codex = _is_codex_command(getattr(record, "recovery_command", None) or getattr(record, "command", ""))

    if not raw_session_id:
        reasons.append(_reason("session_id_missing", "durable pane id is absent from the registry row", "identity"))
        checks["identity"] = _check("invalid", "session identifier is missing")
    elif _SESSION_ID.fullmatch(session_id) is None:
        reasons.append(_reason("session_id_invalid", "durable pane id is not a safe session identifier", "identity"))
        checks["identity"] = _check("invalid", "session identifier is malformed")
    else:
        checks["identity"] = _check("pass", "durable pane identifier is present")
    if _is_true(getattr(record, "external", False)):
        reasons.append(_reason("external_session", "external/adopted panes are never cold-recovered by TermDeck", "lifecycle"))
        checks["lifecycle"] = _check("not_applicable", "external pane is outside managed recovery")
    elif status == "archived":
        reasons.append(_reason("archived_session", "archived pane is excluded from recovery", "lifecycle"))
        checks["lifecycle"] = _check("not_applicable", "pane is archived")
    elif status in _LAYOUT_STATUSES:
        reasons.append(_reason("layout_only", f"status {status!r} records layout, not previously running work", "lifecycle"))
        checks["lifecycle"] = _check("not_applicable", f"pane status is {status}")
    elif status not in _ACTIVE_STATUSES:
        reasons.append(_reason("status_invalid", f"status {status!r} is not a recognized recovery status", "lifecycle"))
        checks["lifecycle"] = _check("invalid", "pane status is not recognized")
    else:
        checks["lifecycle"] = _check("pass", f"status {status} can represent prior work")
        if status in {"running", "paused"}:
            reasons.append(
                _reason(
                    "runtime_liveness_unverified",
                    "running/paused status requires authoritative tmux reconciliation before cold recovery",
                    "lifecycle",
                )
            )
            checks["lifecycle"] = _check(
                "deferred",
                "remembered runtime status cannot prove that the pane is dead",
            )

    intent = _normalise_intent(getattr(record, "recovery_intent", "legacy"))
    if intent == "inactive":
        reasons.append(_reason("inactive_intent", "durable recovery intent explicitly disables cold recovery", "recovery_intent"))
        checks["recovery_intent"] = _check("not_applicable", "intent is inactive")
    elif intent == "legacy":
        reasons.append(_reason("recovery_intent_missing", "durable active recovery intent has not been recorded", "recovery_intent"))
        checks["recovery_intent"] = _check("deferred", "intent is legacy/ambiguous")
    else:
        checks["recovery_intent"] = _check("pass", "durable recovery intent is active")

    recipe_state, recipe, recipe_reason, recipe_check = _recipe_observation(record)
    checks["recipe"] = recipe_check
    if recipe_reason is not None:
        reasons.append(recipe_reason)

    cwd_state, exact_cwd, cwd_reason, cwd_check = _cwd_observation(record, recipe)
    checks["cwd"] = cwd_check
    if cwd_reason is not None:
        reasons.append(cwd_reason)

    auth_state, auth_reason, auth_check = _auth_observation(record, native_codex=native_codex)
    checks["auth_scope"] = auth_check
    if auth_reason is not None:
        reasons.append(auth_reason)

    rollout_state, rollout_reason, rollout_check = _rollout_gate(
        record,
        recipe,
        exact_cwd,
        rollout_root=rollout_root,
        rollout_states=rollout_states,
        max_rollout_files=max_rollout_files,
    )
    checks["codex_rollout"] = rollout_check
    if rollout_reason is not None:
        reasons.append(rollout_reason)

    storage_state, storage_reason, storage_check = _storage_observation(environment)
    checks["storage_attestation"] = storage_check
    if storage_reason is not None:
        reasons.append(storage_reason)

    keys = _target_keys(record, recipe)
    claim_state, claim_reason, claim_check = _claim_observation(connection, keys, now=now)
    checks["coordinator_claimability"] = claim_check
    if claim_reason is not None:
        reasons.append(claim_reason)

    if not reasons:
        classification = "eligible"
        primary = _reason("all_gates_passed", "all required recovery gates passed without starting a process", "qualification")
        all_reasons = (primary,)
    else:
        classification = _failure_classification(reasons)
        primary = reasons[0]
        all_reasons = tuple(reasons)
    queued = None
    if intent != "active" and exact_cwd:
        queued = _queued_intent(record, exact_cwd)
    return QualificationRow(
        row_number=row_number,
        session_id=session_id,
        name=name,
        status=status,
        classification=classification,
        primary_reason=primary,
        reasons=all_reasons,
        checks=checks,
        queued_intent=queued,
        target_keys=keys,
    )


def _apply_target_conflicts(rows: list[QualificationRow]) -> list[QualificationRow]:
    """Defer active rows that would compete for one coordinator target."""

    by_target: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.classification == "skipped":
            continue
        for target_key in row.target_keys:
            by_target.setdefault(target_key, []).append(index)
    conflicts = {
        index
        for indexes in by_target.values()
        if len(indexes) > 1
        for index in indexes
    }
    if not conflicts:
        return rows
    result = list(rows)
    for index in sorted(conflicts):
        row = result[index]
        conflict = _reason(
            "coordinator_target_conflict",
            "another non-skipped pane in this audit has the same durable recovery target",
            "coordinator_claimability",
        )
        reasons = row.reasons + (conflict,)
        checks = dict(row.checks)
        checks["coordinator_claimability"] = _check(
            "deferred",
            "same-audit target conflict requires single-flight coordinator admission",
        )
        classification = _failure_classification(reasons)
        primary = conflict if row.classification == "eligible" else row.primary_reason
        result[index] = replace(
            row,
            classification=classification,
            primary_reason=primary,
            reasons=reasons,
            checks=checks,
        )
    return result


def _empty_error_report(path: Path, error: str) -> RecoveryQualificationReport:
    return RecoveryQualificationReport(db_path=str(path), panes=(), ok=False, error=error)


def _qualify_recovery_connection(
    path: Path,
    connection: sqlite3.Connection,
    *,
    values: Mapping[str, str],
    rollout_root: str | Path | None,
    rollout_states: Mapping[str, object] | None,
    max_rollout_files: int,
    now: float | None,
) -> RecoveryQualificationReport:
    _schema_columns(connection)
    try:
        rows = connection.execute(
            "SELECT rowid AS __qualification_rowid__, * FROM sessions"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = connection.execute("SELECT * FROM sessions").fetchall()
    audit_now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    records = [_record_from_row(dict(row)) for row in rows]
    qualified = [
        _qualify_row(
            index,
            record,
            connection,
            environment=values,
            rollout_root=rollout_root,
            rollout_states=rollout_states,
            max_rollout_files=max_rollout_files,
            now=audit_now,
        )
        for index, record in enumerate(records, start=1)
    ]
    return RecoveryQualificationReport(
        db_path=str(path),
        panes=tuple(_apply_target_conflicts(qualified)),
    )


def qualify_recovery(
    db_path: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    rollout_root: str | Path | None = None,
    rollout_states: Mapping[str, object] | None = None,
    max_rollout_files: int = _MAX_ROLLOUT_FILES,
    now: float | None = None,
    readonly_retry_attempts: int = 3,
    readonly_retry_delay_seconds: float = 0.025,
) -> RecoveryQualificationReport:
    """Audit a primary or explicit registry without mutating it.

    ``environment``, ``rollout_states``, and ``now`` are injectable so tests
    and offline diagnostics can prove each gate without starting a backend.
    Missing rollout/attestation/claim evidence is reported as deferred; bad
    identity or contradictory durable evidence is fail-closed.
    """

    path = Path(db_path).expanduser() if db_path is not None else default_db_path()
    values = os.environ if environment is None else environment
    try:
        refreshed = read_only_refresh(
            path,
            lambda connection: _qualify_recovery_connection(
                Path(path),
                connection,
                values=values,
                rollout_root=rollout_root,
                rollout_states=rollout_states,
                max_rollout_files=max_rollout_files,
                now=now,
            ),
            operation="recovery qualification",
            opener=_open_read_only,
            attempts=readonly_retry_attempts,
            delay_seconds=readonly_retry_delay_seconds,
        )
        return replace(
            refreshed.value,
            source_data_version=refreshed.data_version_before,
            source_data_version_after=refreshed.data_version_after,
        )
    except SQLiteReadOnlyError as exc:
        return _empty_error_report(path, str(exc))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return _empty_error_report(path, str(exc))


def audit_recovery(*args: object, **kwargs: object) -> RecoveryQualificationReport:
    """Compatibility alias for callers that name the operation an audit."""

    return qualify_recovery(*args, **kwargs)  # type: ignore[arg-type]


__all__ = [
    "CLASSIFICATIONS",
    "QualificationCheck",
    "QualificationReason",
    "QualificationRow",
    "RecoveryQualificationReport",
    "audit_recovery",
    "qualify_recovery",
]
