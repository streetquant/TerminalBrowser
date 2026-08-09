from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .liveness import merge_confirmed_pane_snapshot
from .models import ResourceSnapshot, SessionRecord
from .resources import ResourceSampler
from .session_store import SessionStore, utc_now
from .tmux_backend import TmuxBackend, TmuxError


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
        return "recovery-pending", visible, None, "exact recovery is still pending"

    if before == "stopped":
        return "stopped", visible, None, "already stopped"

    if (
        recover_missing
        and not getattr(record, "external", False)
        and before in {"running", "paused", "missing", "remembered", "start-error"}
    ):
        # A host crash removes tmux along with its child processes. Preserve a
        # distinct recovery state so startup cannot confuse a lost process with
        # a deliberately new tab and silently replay its launch command.
        return "recovery-pending", visible, None, "missing managed session requires exact recovery"

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
    ``recover_missing`` is enabled, missing managed sessions become restartable
    ``new`` records without changing their visibility or layout. This function
    never starts new processes and it never exposes backend commands.
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
