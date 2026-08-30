from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .liveness import merge_confirmed_pane_snapshot
from .models import (
    RECOVERY_INTENT_ACTIVE,
    RECOVERY_INTENT_INACTIVE,
    RECOVERY_INTENT_LEGACY,
    ResourceSnapshot,
    SessionRecord,
)
from .resources import ResourceSampler
from .session_recipe import (
    SessionRecipeError,
    automatic_recovery_allowed,
    recipe_from_record,
)
from .session_store import SessionStore, utc_now
from .tmux_backend import TmuxBackend, TmuxError


# These are the only durable states that can represent work which was already
# running before a cold restart.  ``new`` and ``remembered`` are deliberate
# non-started states; treating either as a recovery candidate silently turns a
# saved layout into a process launcher.
_ACTIVE_RECOVERY_STATUSES = frozenset(
    {"running", "paused", "missing", "recovery-pending", "start-error"}
)
_NEVER_STARTED_STATUSES = frozenset({"new", "remembered"})
_TERMINAL_STATUSES = frozenset({"stopped", "archived"})
_ACTIVE_INTENT_VALUES = frozenset(
    {"active", "eligible", "recover", "recovery", "resume", "running", "true", "yes", "1"}
)
_INACTIVE_INTENT_VALUES = frozenset(
    {
        "archived",
        "inactive",
        "manual",
        "never",
        "stopped",
        "terminated",
        "false",
        "no",
        "0",
    }
)


class BackendLike(Protocol):
    def session_exists(self, record: SessionRecord) -> bool: ...
    def pane_pid(self, record: SessionRecord) -> int | None: ...


class SamplerLike(Protocol):
    def snapshot(self, root_pid: int | None) -> ResourceSnapshot: ...


@dataclass(frozen=True)
class ReconcileRow:
    """One registry repair decision for a browser-like terminal task."""

    id: str
    name: str
    tag: str
    window_id: str
    window_title: str
    before_status: str
    after_status: str
    before_visible: bool
    after_visible: bool
    before_pid: int | None
    after_pid: int | None
    live_session: bool
    live_process: bool
    changed: bool
    planned: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "window_id": self.window_id,
            "window_title": self.window_title,
            "before_status": self.before_status,
            "after_status": self.after_status,
            "before_visible": self.before_visible,
            "after_visible": self.after_visible,
            "before_pid": self.before_pid,
            "after_pid": self.after_pid,
            "live_session": self.live_session,
            "live_process": self.live_process,
            "changed": self.changed,
            "planned": self.planned,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconcileResult:
    dry_run: bool
    count: int
    changed: int
    rows: tuple[ReconcileRow, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "action": "reconcile",
            "ok": True,
            "dry_run": self.dry_run,
            "count": self.count,
            "changed": self.changed,
            "sessions": [row.as_dict() for row in self.rows],
        }


def _observe_record(record: SessionRecord, backend: BackendLike, sampler: SamplerLike) -> tuple[bool, int | None, ResourceSnapshot]:
    try:
        live_session = backend.session_exists(record)
    except TmuxError:
        live_session = False
    if not live_session:
        return False, None, sampler.snapshot(None)
    try:
        pid = backend.pane_pid(record)
    except TmuxError:
        pid = None
    snapshot = sampler.snapshot(pid)
    snapshot = merge_confirmed_pane_snapshot(
        snapshot,
        pane_pid=pid,
        pane_confirmed=pid is not None and not getattr(backend, "pane_snapshot_stale", False),
    )
    return True, pid, snapshot


def _sample_many(
    sampler: SamplerLike,
    root_pids: list[int | None],
    *,
    refresh_limit: int | None,
) -> list[ResourceSnapshot]:
    sample_many = getattr(sampler, "snapshots", None)
    if callable(sample_many):
        if refresh_limit is None:
            return list(sample_many(root_pids))
        try:
            return list(sample_many(root_pids, refresh_limit=refresh_limit))
        except TypeError:
            pass
    return [sampler.snapshot(pid) for pid in root_pids]


def _automatic_recovery_available(record: SessionRecord) -> bool:
    if getattr(record, "external", False):
        return False
    try:
        return automatic_recovery_allowed(recipe_from_record(record))
    except SessionRecipeError:
        return False


def _durable_recovery_intent(record: SessionRecord) -> bool | None:
    """Read an optional durable lifecycle intent without trusting free text.

    Older registries do not have an intent column and therefore fall back to
    their explicit status below.  Newer stores may expose the field as
    ``recovery_intent`` (or one of the migration aliases) on the record.  An
    unknown value is fail-closed; a missing value remains ``None`` so legacy
    rows retain their status-based compatibility behavior.
    """

    missing = object()
    for field in ("recovery_intent", "startup_recovery_intent", "active_intent"):
        value = getattr(record, field, missing)
        if value is missing or value is None:
            continue
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized == RECOVERY_INTENT_LEGACY:
            return None
        if normalized in _ACTIVE_INTENT_VALUES:
            return True
        if normalized in _INACTIVE_INTENT_VALUES:
            return False
        return False
    return None


def startup_recovery_allowed(record: SessionRecord) -> bool:
    """Return whether a missing payload may be relaunched automatically.

    This is intentionally stricter than ordinary live-tab reconciliation.  A
    live manual tab may be reattached by the caller after a successful live
    observation, but a cold startup must have both an active durable state and
    an automatic recipe.  Explicit stops, never-started rows, external rows,
    archived rows, and malformed/missing recipes are never relaunched.
    """

    if bool(getattr(record, "external", False)):
        return False
    status = str(getattr(record, "status", "")).strip().casefold()
    if status == "archived" or status in _NEVER_STARTED_STATUSES:
        return False
    intent = _durable_recovery_intent(record)
    if intent is False:
        return False
    if status == "stopped":
        # A runtime reconciliation may have recorded a missing active pane as
        # stopped.  Only an explicit active intent may turn that status back
        # into a startup recovery candidate; an explicit stop remains inactive.
        return intent is True and _automatic_recovery_available(record)
    if status not in _ACTIVE_RECOVERY_STATUSES:
        return False
    # A start error can describe an initial launch that never became active.
    # Only a durable active intent can distinguish it from a retryable crash.
    if status == "start-error" and intent is not True:
        return False
    return _automatic_recovery_available(record)


def startup_restore_candidates(
    records: Iterable[SessionRecord],
    *,
    live_session_ids: Iterable[str] = (),
) -> list[SessionRecord]:
    """Select cold-start rows without turning layout records into processes.

    ``live_session_ids`` comes only from the just-completed authoritative
    reconciliation.  It permits reattaching an already-live manual tab while
    still preventing a missing manual tab from being recreated.  All missing
    rows use :func:`startup_recovery_allowed`.
    """

    live_ids = {str(session_id) for session_id in live_session_ids}
    selected: list[SessionRecord] = []
    for record in records:
        if bool(getattr(record, "external", False)):
            continue
        status = str(getattr(record, "status", "")).strip().casefold()
        if status == "archived" or status in _NEVER_STARTED_STATUSES:
            continue
        intent = _durable_recovery_intent(record)
        if intent is False:
            continue
        if str(getattr(record, "id", "")) in live_ids:
            # Reattachment is safe only for the live state proven by the
            # reconciliation pass.  It is not a permission to recreate a
            # missing manual/never-started row.
            if status in {"running", "paused"}:
                selected.append(record)
            continue
        if startup_recovery_allowed(record):
            selected.append(record)
    return selected


def _desired_state(
    record: SessionRecord,
    *,
    live_session: bool,
    pid: int | None,
    snapshot: ResourceSnapshot,
    hide_missing: bool,
    recover_missing: bool,
) -> tuple[str, bool, int | None, str]:
    before = record.status.casefold()
    visible = bool(record.visible)

    if before == "archived":
        return record.status, visible, record.last_pid, "archived record skipped"

    # An explicit stop wins over a stale tmux listing.  Reconciliation should
    # never convert a user stop back to a running row merely because cleanup of
    # the old pane was delayed.
    if before == "stopped":
        if recover_missing and startup_recovery_allowed(record):
            return (
                "recovery-pending",
                visible,
                None,
                "stopped observation retains an explicit active recovery intent",
            )
        return "stopped", visible, None, "explicitly stopped record preserved"

    # ``new`` and ``remembered`` are layout-only rows.  In startup mode they
    # remain exactly that even if a stale backend name happens to exist.
    if recover_missing and before in _NEVER_STARTED_STATUSES:
        return record.status, visible, None, "not started record preserved"

    if live_session and snapshot.exists:
        status = "paused" if snapshot.state == "T" or (snapshot.state == "unknown" and before == "paused") else "running"
        detail = "live session observed; resource sample deferred" if snapshot.state == "unknown" else "live session observed"
        return status, visible, pid, detail

    if live_session and pid is not None and not snapshot.exists:
        return "missing", visible, pid, "backend reported a pane pid but the process was not found"

    if live_session and pid is None:
        status = "missing" if before not in {"new", "stopped"} else before
        return status, visible, None, "live session exists but no pane pid was found"

    if before == "new":
        return "new", visible, None, "not started yet"

    if before == "recovery-pending":
        if _automatic_recovery_available(record):
            return (
                "recovery-pending",
                visible,
                None,
                "durable generic recovery is still pending",
            )
        return (
            "recovery-pending",
            visible,
            None,
            "recovery is pending but no valid automatic recipe is available",
        )

    if (
        recover_missing
        and before in _ACTIVE_RECOVERY_STATUSES
        and startup_recovery_allowed(record)
    ):
        # A host crash removes tmux along with its child processes. Preserve a
        # distinct recovery state and let the backend replay only the canonical,
        # digest-bound generic recipe. No application/tool parser is required.
        return (
            "recovery-pending",
            visible,
            None,
            "missing managed session has an authorized durable recovery recipe",
        )

    # Browser-like recovery rule: if a remembered running/paused tab has lost
    # its hidden backend session, do not leave it looking restorable.
    after_visible = False if hide_missing else visible
    return "stopped", after_visible, None, "missing backend session marked stopped"


def reconcile_records(
    records: Iterable[SessionRecord],
    *,
    store: SessionStore | None = None,
    backend: BackendLike | None = None,
    sampler: SamplerLike | None = None,
    dry_run: bool = False,
    hide_missing: bool = True,
    recover_missing: bool = False,
    resource_sample_limit: int | None = None,
) -> ReconcileResult:
    """Repair stale remembered session state against the hidden backend.

    TermDeck's browser registry may survive longer than the actual tmux/process
    tree.  This function reconciles the registry with the live backend before a
    restore or when the user asks for a CLI health repair. When
    ``recover_missing`` is enabled, missing managed sessions with a valid
    automatic recipe become ``recovery-pending`` without changing visibility or
    layout. This function never starts new processes and never exposes backend
    commands. External, stopped, archived, manual, and corrupt recipes remain
    fail-closed.
    """

    backend = backend or TmuxBackend()
    sampler = sampler or ResourceSampler()
    rows: list[ReconcileRow] = []
    record_list = list(records)

    # GUI startup may restore dozens of tabs. Prefer two bounded backend
    # snapshots over one has-session + pane lookup per record.
    live_by_id: dict[str, bool] | None = None
    pids_by_id: dict[str, int | None] | None = None
    exists_many = getattr(backend, "session_exists_many", None)
    if callable(exists_many):
        try:
            live_by_id = dict(exists_many(record_list))
        except TmuxError:
            live_by_id = None
    pane_pids = getattr(backend, "pane_pids", None)
    if live_by_id is not None and callable(pane_pids):
        live_records = [record for record in record_list if live_by_id.get(record.id, False)]
        try:
            pids_by_id = dict(pane_pids(live_records))
        except TmuxError:
            pids_by_id = None

    observations: list[tuple[SessionRecord, bool, int | None, ResourceSnapshot]] = []
    if live_by_id is None:
        for record in record_list:
            live_session, pid, snapshot = _observe_record(record, backend, sampler)
            observations.append((record, live_session, pid, snapshot))
    else:
        resolved: list[tuple[SessionRecord, bool, int | None]] = []
        for record in record_list:
            live_session = bool(live_by_id.get(record.id, False))
            if not live_session:
                pid = None
            else:
                try:
                    pid = pids_by_id.get(record.id) if pids_by_id is not None else backend.pane_pid(record)
                except TmuxError:
                    pid = None
            resolved.append((record, live_session, pid))
        if getattr(backend, "pane_snapshot_stale", False):
            # A failed tmux control-plane query returned remembered PIDs. Do
            # not use those stale identifiers as proof that sessions died or
            # changed state; wait for a successful bounded pane snapshot.
            snapshots = [
                ResourceSnapshot(pid=pid, exists=live_session, state="unknown")
                for _record, live_session, pid in resolved
            ]
        else:
            snapshots = _sample_many(
                sampler,
                [pid if live_session else None for _record, live_session, pid in resolved],
                refresh_limit=resource_sample_limit,
            )
            snapshots = [
                merge_confirmed_pane_snapshot(
                    snapshot,
                    pane_pid=pid,
                    pane_confirmed=live_session and pid is not None,
                )
                for (_record, live_session, pid), snapshot in zip(resolved, snapshots)
            ]
        observations = [
            (record, live_session, pid, snapshot)
            for (record, live_session, pid), snapshot in zip(resolved, snapshots)
        ]

    pending_updates: list[tuple[str, str, bool, int | None]] = []
    for record, live_session, pid, snapshot in observations:
        after_status, after_visible, after_pid, detail = _desired_state(
            record,
            live_session=live_session,
            pid=pid,
            snapshot=snapshot,
            hide_missing=hide_missing,
            recover_missing=recover_missing,
        )
        changed = (
            after_status != record.status
            or after_visible != record.visible
            or after_pid != record.last_pid
        )
        if changed and store is not None and not dry_run:
            pending_updates.append((record.id, after_status, after_visible, after_pid))
        rows.append(
            ReconcileRow(
                id=record.id,
                name=record.name,
                tag=record.tag,
                window_id=record.window_id,
                window_title=record.window_title,
                before_status=record.status,
                after_status=after_status,
                before_visible=record.visible,
                after_visible=after_visible,
                before_pid=record.last_pid,
                after_pid=after_pid,
                live_session=live_session,
                live_process=bool(snapshot.exists),
                changed=changed,
                planned=dry_run,
                detail=detail,
            )
        )

    if pending_updates and store is not None and not dry_run:
        update_many = getattr(store, "update_runtime_states", None)
        if callable(update_many):
            update_many(pending_updates)
        else:
            for session_id, status, visible, last_pid in pending_updates:
                store.update(
                    session_id,
                    status=status,
                    visible=visible,
                    last_pid=last_pid,
                    last_opened_at=utc_now(),
                )

    return ReconcileResult(
        dry_run=dry_run,
        count=len(rows),
        changed=sum(1 for row in rows if row.changed),
        rows=tuple(rows),
    )


def reconcile_store(
    store: SessionStore,
    *,
    backend: BackendLike | None = None,
    sampler: SamplerLike | None = None,
    dry_run: bool = False,
    hide_missing: bool = True,
    recover_missing: bool = False,
    include_archived: bool = False,
    tag: str | None = None,
    resource_sample_limit: int | None = None,
) -> ReconcileResult:
    records = store.list(include_archived=include_archived)
    if tag:
        records = [record for record in records if record.tag == tag]
    return reconcile_records(
        records,
        store=store,
        backend=backend,
        sampler=sampler,
        dry_run=dry_run,
        hide_missing=hide_missing,
        recover_missing=recover_missing,
        resource_sample_limit=resource_sample_limit,
    )
