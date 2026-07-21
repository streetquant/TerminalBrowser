from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .models import SessionRecord
from .resources import safe_codex_recovery_command
from .session_store import SessionStore

_MAX_METADATA_LINE_BYTES = 65_536
_DEFAULT_MAX_ARCHIVE_FILES = 20_000
_NEAR_TIME_SECONDS = 15 * 60
_SHELL_NAMES = frozenset(
    {
        "bash",
        "csh",
        "dash",
        "elvish",
        "fish",
        "ksh",
        "nu",
        "sh",
        "tcsh",
        "zsh",
    }
)


@dataclass(frozen=True)
class ArchivedCodexSession:
    session_id: str
    cwd: str
    created_at: float
    modified_at: float
    path: str


@dataclass(frozen=True)
class LegacyRecoveryDecision:
    record_id: str
    action: str
    detail: str
    session_id: str | None = None
    recovery_cwd: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.record_id,
            "action": self.action,
            "detail": self.detail,
            "session_id": self.session_id,
            "recovery_cwd": self.recovery_cwd,
        }


@dataclass(frozen=True)
class LegacyRecoveryResult:
    metadata_updates: int
    runtime_updates: int
    decisions: tuple[LegacyRecoveryDecision, ...]

    @property
    def exact_count(self) -> int:
        return sum(1 for row in self.decisions if row.action == "exact-codex")

    @property
    def shell_count(self) -> int:
        return sum(1 for row in self.decisions if row.action == "shell-fallback")

    @property
    def unresolved_count(self) -> int:
        return sum(1 for row in self.decisions if row.action == "unresolved")

    def as_dict(self) -> dict[str, object]:
        return {
            "metadata_updates": self.metadata_updates,
            "runtime_updates": self.runtime_updates,
            "exact_count": self.exact_count,
            "shell_count": self.shell_count,
            "unresolved_count": self.unresolved_count,
            "sessions": [row.as_dict() for row in self.decisions],
        }


def _canonical_uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _normalized_cwd(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    return os.path.normpath(str(path))


def default_codex_archive_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return home / "sessions"


def scan_codex_archive(
    root: str | Path | None = None,
    *,
    max_files: int = _DEFAULT_MAX_ARCHIVE_FILES,
) -> tuple[ArchivedCodexSession, ...]:
    """Read bounded session metadata without inspecting transcript content."""

    archive_root = Path(root).expanduser() if root else default_codex_archive_root()
    if not archive_root.is_dir():
        return ()
    paths = [path for path in archive_root.rglob("*.jsonl") if path.is_file()]
    clean_limit = max(1, int(max_files))
    if len(paths) > clean_limit:
        paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        paths = paths[:clean_limit]

    by_id: dict[str, ArchivedCodexSession] = {}
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline(_MAX_METADATA_LINE_BYTES + 1)
            if not first_line or len(first_line) > _MAX_METADATA_LINE_BYTES:
                continue
            payload = json.loads(first_line).get("payload", {})
            if not isinstance(payload, dict) or payload.get("source") != "cli":
                continue
            session_id = _canonical_uuid(payload.get("id") or payload.get("session_id"))
            cwd = _normalized_cwd(payload.get("cwd"))
            created_at = _timestamp(payload.get("timestamp"))
            if session_id is None or cwd is None or created_at is None:
                continue
            modified_at = path.stat().st_mtime
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        candidate = ArchivedCodexSession(
            session_id=session_id,
            cwd=cwd,
            created_at=created_at,
            modified_at=modified_at,
            path=str(path),
        )
        previous = by_id.get(session_id)
        if previous is None or candidate.modified_at > previous.modified_at:
            by_id[session_id] = candidate
    return tuple(sorted(by_id.values(), key=lambda row: row.modified_at, reverse=True))


def _exact_resume_id(command: str | None) -> str | None:
    raw = (command or "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        return None
    if "resume" not in argv:
        return None
    index = argv.index("resume")
    if len(argv) != index + 2:
        return None
    return _canonical_uuid(argv[-1])


def _launcher_only_codex(command: str | None) -> bool:
    raw = (command or "").strip()
    if not raw:
        return False
    try:
        argv = shlex.split(raw)
    except ValueError:
        return False
    safe = safe_codex_recovery_command(argv)
    return safe is not None and " resume " not in f" {safe} "


def _known_shell(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return len(argv) == 1 and Path(argv[0]).name.casefold() in _SHELL_NAMES


def _generic_cwd(cwd: str) -> bool:
    normalized = os.path.normpath(cwd)
    return normalized in {os.path.normpath(str(Path.home())), "/", "/home", "/8tb", "/storage"}


def _record_timestamp(record: SessionRecord) -> float | None:
    return _timestamp(record.created_at)


def _nearest_candidate(
    record: SessionRecord,
    candidates: Sequence[ArchivedCodexSession],
) -> ArchivedCodexSession | None:
    created_at = _record_timestamp(record)
    if created_at is None or not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda row: (
            min(abs(row.created_at - created_at), abs(row.modified_at - created_at)),
            -row.modified_at,
        ),
    )
    best = ranked[0]
    distance = min(abs(best.created_at - created_at), abs(best.modified_at - created_at))
    if distance > _NEAR_TIME_SECONDS:
        return None
    if len(ranked) > 1:
        second_distance = min(
            abs(ranked[1].created_at - created_at),
            abs(ranked[1].modified_at - created_at),
        )
        if second_distance <= _NEAR_TIME_SECONDS and second_distance - distance < 60:
            return None
    return best


def plan_legacy_recovery(
    records: Iterable[SessionRecord],
    archive: Sequence[ArchivedCodexSession],
) -> tuple[LegacyRecoveryDecision, ...]:
    """Resolve legacy tabs conservatively and prevent concurrent UUID reuse."""

    record_list = list(records)
    occupied = {
        session_id
        for record in record_list
        if (session_id := _exact_resume_id(record.recovery_command)) is not None
    }
    by_cwd: dict[str, list[ArchivedCodexSession]] = {}
    for row in archive:
        by_cwd.setdefault(row.cwd, []).append(row)
    for rows in by_cwd.values():
        rows.sort(key=lambda row: row.modified_at, reverse=True)

    targets = [
        record
        for record in record_list
        if record.visible
        and record.status.casefold() in {"start-error", "recovery-pending"}
        and _exact_resume_id(record.recovery_command) is None
        and not getattr(record, "external", False)
    ]
    targets.sort(
        key=lambda row: (
            row.status.casefold() != "recovery-pending",
            row.name.casefold() != Path(row.cwd).name.casefold(),
            row.created_at,
            row.id,
        )
    )

    decisions: list[LegacyRecoveryDecision] = []
    for record in targets:
        cwd = _normalized_cwd(record.cwd) or record.cwd
        all_candidates = by_cwd.get(cwd, [])
        candidates = [row for row in all_candidates if row.session_id not in occupied]
        launcher_evidence = _launcher_only_codex(record.recovery_command)
        chosen: ArchivedCodexSession | None = None
        detail = ""

        if launcher_evidence and candidates and not _generic_cwd(cwd):
            chosen = candidates[0]
            detail = "launcher-only Codex evidence matched the newest CLI archive session in the exact project directory"
        elif candidates:
            chosen = _nearest_candidate(record, candidates)
            if chosen is not None:
                detail = "tab creation time uniquely matched a CLI archive session in the exact working directory"

        if chosen is not None:
            occupied.add(chosen.session_id)
            decisions.append(
                LegacyRecoveryDecision(
                    record_id=record.id,
                    action="exact-codex",
                    detail=detail,
                    session_id=chosen.session_id,
                    recovery_cwd=cwd,
                )
            )
            continue

        if _known_shell(record.command):
            reason = (
                "exact Codex UUID was ambiguous; restored the saved shell surface without claiming conversation recovery"
                if launcher_evidence or all_candidates
                else "no archived Codex session matched; restored the saved shell surface"
            )
            decisions.append(
                LegacyRecoveryDecision(
                    record_id=record.id,
                    action="shell-fallback",
                    detail=reason,
                    recovery_cwd=cwd,
                )
            )
        else:
            decisions.append(
                LegacyRecoveryDecision(
                    record_id=record.id,
                    action="unresolved",
                    detail="no unambiguous archived Codex UUID or safe shell fallback was available",
                    recovery_cwd=cwd,
                )
            )
    return tuple(decisions)


def apply_legacy_recovery(
    store: SessionStore,
    decisions: Iterable[LegacyRecoveryDecision],
) -> LegacyRecoveryResult:
    """Persist exact mappings and make safe shell fallbacks startup-eligible."""

    rows = tuple(decisions)
    exact_updates = [
        (
            row.record_id,
            f"codex --yolo resume {row.session_id}",
            row.recovery_cwd,
        )
        for row in rows
        if row.action == "exact-codex" and row.session_id is not None
    ]
    metadata_updates = store.update_recovery_metadata(exact_updates) if exact_updates else 0
    runtime_rows = [
        (row.record_id, "recovery-pending", True, None)
        for row in rows
        if row.action == "exact-codex"
    ]
    runtime_rows.extend(
        (row.record_id, "new", True, None)
        for row in rows
        if row.action == "shell-fallback"
    )
    runtime_updates = store.update_runtime_states(runtime_rows) if runtime_rows else 0
    return LegacyRecoveryResult(metadata_updates, runtime_updates, rows)


def repair_legacy_recovery(
    store: SessionStore,
    *,
    archive_root: str | Path | None = None,
    max_files: int = _DEFAULT_MAX_ARCHIVE_FILES,
) -> LegacyRecoveryResult:
    records = store.list(include_archived=False, include_stopped=False)
    archive = scan_codex_archive(archive_root, max_files=max_files)
    decisions = plan_legacy_recovery(records, archive)
    return apply_legacy_recovery(store, decisions)
