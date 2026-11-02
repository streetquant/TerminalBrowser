"""Bounded, durable recovery for managed TermDeck sessions."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import shlex
import sqlite3
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable
from uuid import UUID, uuid4

from .auth_scope_rebind import AuthScopeRebindProof, rebind_auth_scope
from .models import RECOVERY_INTENT_ACTIVE
from .session_reconcile import startup_recovery_allowed
from .resources import process_tree_contains_codex
from .session_recipe import SessionRecipeError, recipe_from_record, record_recipe_fields
from .session_store import SessionStore, utc_now
from .tmux_backend import TmuxError


_NO_QUIESCENCE_OBSERVATION = object()


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of one durable recovery request."""

    session_id: str
    mode: str
    state: str
    pid: int | None = None
    created: bool = False
    attempt: int = 0
    error: str | None = None
    action: str | None = None
    command: str | None = None
    cwd: str | None = None
    clear_recovery: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class RecoverySubmission:
    """Immediate acknowledgement returned when work enters the coordinator."""

    session_id: str
    state: str
    accepted: bool


@dataclass
class _Listener:
    on_ready: Callable[[RecoveryResult], object] | None
    on_failed: Callable[[RecoveryResult], object] | None
    session_id: str = ""


@dataclass
class _Request:
    record: object
    mode: str
    initial_grid: tuple[int, int] | None
    listeners: list[_Listener]
    attempt: int = 0
    future: object | None = None
    target_keys: tuple[str, ...] = ()
    claim_token: str | None = None
    next_claim_renewal: float = 0.0
    following_claim: bool = False
    followed_session_id: str = ""
    followed_attempt: int = 0
    cancelled: threading.Event = field(default_factory=threading.Event)
    claim_lost: threading.Event = field(default_factory=threading.Event)


class _AdmissionUnavailable(RuntimeError):
    """The durable session state could not be inspected or claimed."""


@dataclass(frozen=True)
class _ClaimHeld:
    """A compatible recovery target is owned by another live coordinator."""

    owner_id: str
    expires_at: float
    session_id: str = ""
    attempt: int = 0


@dataclass(frozen=True)
class _Claim:
    """The durable claim acquired by one recovery request."""

    attempt: int
    token: str
    target_keys: tuple[str, ...]


class RecoveryCoordinator:
    """Admit managed recovery with durable state and bounded concurrency.

    Recovery is bounded by a small process-local pool and by durable
    per-target claims.  Different workspaces can recover concurrently, while
    the same session or underlying tmux/auth/resume target remains single
    flight across coordinator processes.
    """

    _VALID_MODES = frozenset({"restore", "payload"})
    DEFAULT_READINESS_TIMEOUT = 3600.0
    DEFAULT_MAX_WORKERS = 4
    MIN_MAX_WORKERS = 1
    MAX_MAX_WORKERS = 16
    DEFAULT_CLAIM_TTL = 3600.0
    MIN_CLAIM_TTL = 5.0
    MAX_CLAIM_TTL = 86400.0
    WORKER_COUNT_ENV = "TERMDECK_RECOVERY_WORKERS"
    LEGACY_WORKER_COUNT_ENV = "TERMDECK_RECOVERY_MAX_WORKERS"
    CLAIM_TTL_ENV = "TERMDECK_RECOVERY_CLAIM_TTL"

    @classmethod
    def _configured_worker_count(cls, value: object) -> int:
        """Return a safe worker count clamped to the supported pool size."""

        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            return cls.DEFAULT_MAX_WORKERS
        return max(cls.MIN_MAX_WORKERS, min(parsed, cls.MAX_MAX_WORKERS))

    @classmethod
    def _configured_claim_ttl(cls, value: object) -> float:
        """Return a bounded lease duration without disabling claims."""

        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return cls.DEFAULT_CLAIM_TTL
        if not math.isfinite(parsed) or parsed < cls.MIN_CLAIM_TTL:
            return cls.DEFAULT_CLAIM_TTL
        return min(parsed, cls.MAX_CLAIM_TTL)

    @classmethod
    def _configured_readiness_timeout(cls, value: object) -> float:
        """Return a positive finite timeout without disabling coordination."""

        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return cls.DEFAULT_READINESS_TIMEOUT
        if not math.isfinite(parsed) or parsed <= 0:
            return cls.DEFAULT_READINESS_TIMEOUT
        return parsed

    def __init__(
        self,
        backend,
        store: SessionStore | None = None,
        *,
        max_workers: int | None = None,
        readiness_timeout: float | None = None,
        poll_interval: float = 0.05,
        callback_executor: Callable[[Callable[[], object]], object] | None = None,
        store_factory: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], object] = time.sleep,
        claim_ttl: float | None = None,
        wall_clock: Callable[[], float] = time.time,
        allow_auth_scope_rebind: bool = True,
        readiness_failure_grace: float = 0.0,
    ) -> None:
        if max_workers is None:
            max_workers = os.environ.get(
                self.WORKER_COUNT_ENV,
                os.environ.get(
                    self.LEGACY_WORKER_COUNT_ENV,
                    str(self.DEFAULT_MAX_WORKERS),
                ),
            )
        max_workers = self._configured_worker_count(max_workers)
        if readiness_timeout is None:
            configured_timeout = os.environ.get(
                "TERMDECK_RECOVERY_READINESS_TIMEOUT",
                str(self.DEFAULT_READINESS_TIMEOUT),
            )
            readiness_timeout = configured_timeout
        readiness_timeout = self._configured_readiness_timeout(readiness_timeout)
        if poll_interval <= 0:
            raise ValueError("invalid recovery timing")
        self.backend = backend
        self.store = store
        self.readiness_timeout = float(readiness_timeout)
        self.poll_interval = float(poll_interval)
        self.callback_executor = callback_executor
        self.store_factory = store_factory
        self.clock = clock
        self.sleeper = sleeper
        if claim_ttl is None:
            claim_ttl = os.environ.get(self.CLAIM_TTL_ENV, str(self.DEFAULT_CLAIM_TTL))
        self.claim_ttl = self._configured_claim_ttl(claim_ttl)
        self.wall_clock = wall_clock
        self.allow_auth_scope_rebind = bool(allow_auth_scope_rebind)
        if not math.isfinite(readiness_failure_grace) or readiness_failure_grace < 0:
            raise ValueError("invalid readiness failure grace")
        self.readiness_failure_grace = min(float(readiness_failure_grace), self.readiness_timeout)
        self.owner_id = self._new_owner_id()
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._inflight: dict[str, _Request] = {}
        self._inflight_keys: dict[str, str] = {}
        self._follower_wakeup = threading.Event()
        self._follower_thread: threading.Thread | None = None
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="termdeck-recovery",
        )

    @property
    def inflight_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._inflight)

    def submit(
        self,
        record,
        *,
        mode: str = "restore",
        known_live: bool | None = None,
        known_pid: int | None = None,
        initial_grid: tuple[int, int] | None = None,
        on_ready: Callable[[RecoveryResult], object] | None = None,
        on_failed: Callable[[RecoveryResult], object] | None = None,
    ) -> RecoverySubmission:
        """Attach a live target or queue one missing target for recovery."""

        if mode not in self._VALID_MODES:
            raise ValueError(f"unsupported recovery mode: {mode}")
        session_id = str(getattr(record, "id", "")).strip()
        if not session_id:
            return RecoverySubmission(session_id="", state="rejected", accepted=False)
        listener = _Listener(
            on_ready=on_ready,
            on_failed=on_failed,
            session_id=session_id,
        )
        try:
            current = self._read_admission_record(record)
        except Exception as exc:
            result = RecoveryResult(
                session_id=session_id,
                mode=mode,
                state="failed",
                error=f"recovery state is unavailable: {exc}",
            )
            self._deliver(result, listener)
            return RecoverySubmission(session_id=session_id, state="failed", accepted=False)
        if current is None:
            result = RecoveryResult(
                session_id=session_id,
                mode=mode,
                state="skipped",
                error="session is no longer present in the durable registry",
            )
            self._deliver(result, listener)
            return RecoverySubmission(session_id=session_id, state="rejected", accepted=False)
        record = current
        if not startup_recovery_allowed(record):
            result = RecoveryResult(
                session_id=session_id,
                mode=mode,
                state="skipped",
                attempt=int(getattr(record, "recovery_attempts", 0) or 0),
                error="recovery is not durably eligible",
            )
            self._deliver(result, listener)
            return RecoverySubmission(session_id=session_id, state="rejected", accepted=False)
        target_keys = self._single_flight_keys(record)
        with self._lock:
            if self._closed:
                result = RecoveryResult(
                    session_id=session_id,
                    mode=mode,
                    state="failed",
                    error="recovery coordinator is closed",
                )
                self._deliver(result, listener)
                return RecoverySubmission(session_id=session_id, state="closed", accepted=False)
            target_key_set = set(target_keys)
            existing = next(
                (
                    request
                    for request in self._inflight.values()
                    if target_key_set.intersection(request.target_keys)
                ),
                None,
            )
            if existing is not None:
                existing_session_id = str(getattr(existing.record, "id", ""))
                if existing_session_id == session_id and existing.mode == mode:
                    existing.listeners.append(listener)
                    return RecoverySubmission(
                        session_id=session_id,
                        state="deduplicated",
                        accepted=True,
                    )
                if existing_session_id == session_id:
                    result = RecoveryResult(
                        session_id=session_id,
                        mode=mode,
                        state="failed",
                        error="an incompatible recovery mode is already in flight",
                    )
                    self._deliver(result, listener)
                    return RecoverySubmission(
                        session_id=session_id,
                        state="rejected",
                        accepted=False,
                    )
                return self._attach_claim_follower(
                    record,
                    mode=mode,
                    initial_grid=initial_grid,
                    listener=listener,
                    target_keys=target_keys,
                    held=_ClaimHeld(
                        owner_id=self.owner_id,
                        expires_at=float("inf"),
                        session_id=existing_session_id,
                        attempt=existing.attempt,
                    ),
                )
            if (
                known_live is True
                and known_pid is not None
                and int(known_pid) > 1
                and self._known_live_ready(record, int(known_pid))
            ):
                try:
                    claimed, current = self._persist_ready_if_eligible(
                        self.store,
                        record,
                        int(known_pid),
                        target_keys=target_keys,
                    )
                except Exception as exc:
                    result = RecoveryResult(
                        session_id=session_id,
                        mode=mode,
                        state="failed",
                        error=f"recovery state could not be claimed: {exc}",
                    )
                    self._deliver(result, listener)
                    return RecoverySubmission(
                        session_id=session_id,
                        state="failed",
                        accepted=False,
                    )
                if not claimed:
                    if current is None or not startup_recovery_allowed(current):
                        result = RecoveryResult(
                            session_id=session_id,
                            mode=mode,
                            state="skipped",
                            attempt=int(
                                getattr(current or record, "recovery_attempts", 0)
                                or 0
                            ),
                            error="recovery became ineligible before live attach",
                        )
                        self._deliver(result, listener)
                        return RecoverySubmission(
                            session_id=session_id,
                            state="rejected",
                            accepted=False,
                        )
                    return self._attach_claim_follower(
                        current,
                        mode=mode,
                        initial_grid=initial_grid,
                        listener=listener,
                        target_keys=target_keys,
                    )
                result = RecoveryResult(
                    session_id=session_id,
                    mode=mode,
                    state="ready",
                    pid=int(known_pid),
                    created=False,
                    attempt=int(getattr(current or record, "recovery_attempts", 0) or 0),
                )
                self._deliver(result, listener)
                return RecoverySubmission(session_id=session_id, state="live", accepted=True)
            try:
                claim = self._mark_pending(record, target_keys)
            except Exception as exc:
                result = RecoveryResult(
                    session_id=session_id,
                    mode=mode,
                    state="failed",
                    error=f"recovery state could not be claimed: {exc}",
                )
                self._deliver(result, listener)
                return RecoverySubmission(session_id=session_id, state="failed", accepted=False)
            if isinstance(claim, _ClaimHeld):
                return self._attach_claim_follower(
                    record,
                    mode=mode,
                    initial_grid=initial_grid,
                    listener=listener,
                    target_keys=target_keys,
                    held=claim,
                )
            if claim is None:
                result = RecoveryResult(
                    session_id=session_id,
                    mode=mode,
                    state="skipped",
                    attempt=int(getattr(record, "recovery_attempts", 0) or 0),
                    error="recovery became ineligible before queue admission",
                )
                self._deliver(result, listener)
                return RecoverySubmission(session_id=session_id, state="rejected", accepted=False)
            attempt = claim.attempt
            self._inflight[session_id] = _Request(
                record=record,
                mode=mode,
                initial_grid=initial_grid,
                listeners=[listener],
                attempt=attempt,
                target_keys=claim.target_keys,
                claim_token=claim.token,
            )
            request = self._inflight[session_id]
            for key in claim.target_keys:
                self._inflight_keys[key] = session_id
            try:
                future = self._executor.submit(self._run, session_id, attempt)
                request.future = future
            except Exception as exc:
                self._inflight.pop(session_id, None)
                for key in claim.target_keys:
                    if self._inflight_keys.get(key) == session_id:
                        self._inflight_keys.pop(key, None)
                result = RecoveryResult(
                    session_id=session_id,
                    mode=mode,
                    state="failed",
                    attempt=attempt,
                    error=str(exc),
                )
                try:
                    self._persist_failure(
                        self.store,
                        record,
                        attempt,
                        str(exc),
                        claim_token=claim.token,
                        target_keys=claim.target_keys,
                    )
                except Exception:
                    pass
                self._deliver(result, listener)
                return RecoverySubmission(session_id=session_id, state="failed", accepted=False)
        return RecoverySubmission(session_id=session_id, state="queued", accepted=True)

    def _attach_claim_follower(
        self,
        record,
        *,
        mode: str,
        initial_grid: tuple[int, int] | None,
        listener: _Listener,
        target_keys: tuple[str, ...],
        held: _ClaimHeld | None = None,
    ) -> RecoverySubmission:
        """Subscribe to a foreign durable claim without occupying a worker."""

        session_id = str(getattr(record, "id", ""))
        request = _Request(
            record=record,
            mode=mode,
            initial_grid=initial_grid,
            listeners=[listener],
            attempt=int(getattr(record, "recovery_attempts", 0) or 0),
            target_keys=target_keys,
            following_claim=True,
            followed_session_id=(
                held.session_id if held is not None and held.session_id else session_id
            ),
            followed_attempt=(
                held.attempt
                if held is not None
                else int(getattr(record, "recovery_attempts", 0) or 0)
            ),
        )
        self._inflight[session_id] = request
        for key in target_keys:
            self._inflight_keys.setdefault(key, session_id)
        try:
            self._ensure_follower_monitor()
        except Exception as exc:
            self._inflight.pop(session_id, None)
            for key in target_keys:
                if self._inflight_keys.get(key) == session_id:
                    self._inflight_keys.pop(key, None)
            result = RecoveryResult(
                session_id=session_id,
                mode=mode,
                state="failed",
                attempt=request.attempt,
                error=f"recovery claim could not be followed: {exc}",
            )
            self._deliver(result, listener)
            return RecoverySubmission(
                session_id=session_id,
                state="failed",
                accepted=False,
            )
        self._follower_wakeup.set()
        return RecoverySubmission(
            session_id=session_id,
            state="deduplicated",
            accepted=True,
        )

    def _ensure_follower_monitor(self) -> None:
        thread = self._follower_thread
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=self._follow_foreign_claims,
            name="termdeck-recovery-claims",
            daemon=True,
        )
        self._follower_thread = thread
        thread.start()

    def _complete_inflight(
        self,
        session_id: str,
        result: RecoveryResult,
        *,
        expected: _Request | None = None,
    ) -> bool:
        with self._lock:
            current = self._inflight.get(session_id)
            if current is None or (expected is not None and current is not expected):
                return False
            completed = self._inflight.pop(session_id)
            for key in completed.target_keys:
                if self._inflight_keys.get(key) == session_id:
                    self._inflight_keys.pop(key, None)
        for listener in completed.listeners:
            self._deliver(result, listener)
        return True

    def _follow_foreign_claims(self) -> None:
        worker_store = None
        owned_store = False
        try:
            worker_store, owned_store = self._worker_store()
            if self.store is not None and worker_store is None:
                raise _AdmissionUnavailable("claim-follower session store could not be opened")
            while True:
                with self._lock:
                    if self._closed:
                        return
                    session_ids = tuple(
                        session_id
                        for session_id, request in self._inflight.items()
                        if request.following_claim
                    )
                    if not session_ids:
                        return
                for session_id in session_ids:
                    self._poll_claim_follower(session_id, worker_store)
                self._follower_wakeup.wait(self.poll_interval)
                self._follower_wakeup.clear()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            with self._lock:
                followers = tuple(
                    (session_id, request)
                    for session_id, request in self._inflight.items()
                    if request.following_claim
                )
            for session_id, request in followers:
                self._complete_inflight(
                    session_id,
                    RecoveryResult(
                        session_id=session_id,
                        mode=request.mode,
                        state="failed",
                        attempt=request.attempt,
                        error=f"recovery claim follower failed: {error}",
                    ),
                    expected=request,
                )
        finally:
            self._close_store(worker_store, owned_store)
            with self._lock:
                if self._follower_thread is threading.current_thread():
                    self._follower_thread = None
                restart = not self._closed and any(
                    request.following_claim for request in self._inflight.values()
                )
                if restart:
                    self._ensure_follower_monitor()

    def _poll_claim_follower(self, session_id: str, worker_store) -> None:
        with self._lock:
            request = self._inflight.get(session_id)
            if request is None or not request.following_claim:
                return
        current = self._read_record_from(worker_store, request.record)
        if current is None or not startup_recovery_allowed(current):
            self._complete_inflight(
                session_id,
                RecoveryResult(
                    session_id=session_id,
                    mode=request.mode,
                    state="skipped",
                    attempt=int(getattr(current or request.record, "recovery_attempts", 0) or 0),
                    error="recovery was made inactive while following another owner",
                ),
                expected=request,
            )
            return

        ready_pid = self._followed_ready_pid(current)
        if ready_pid is not None:
            self._complete_inflight(
                session_id,
                RecoveryResult(
                    session_id=session_id,
                    mode=request.mode,
                    state="ready",
                    pid=ready_pid,
                    created=False,
                    attempt=int(getattr(current, "recovery_attempts", 0) or 0),
                ),
                expected=request,
            )
            return

        with self._lock:
            contenders = [
                (candidate_id, candidate)
                for candidate_id, candidate in self._inflight.items()
                if candidate_id != session_id
                and set(request.target_keys).intersection(candidate.target_keys)
            ]
            if any(not candidate.following_claim for _id, candidate in contenders):
                return
            follower_ids = [session_id, *(
                candidate_id
                for candidate_id, candidate in contenders
                if candidate.following_claim
            )]
            if session_id != min(follower_ids):
                return

        held = self._foreign_claim(worker_store, request.target_keys)
        if held is not None:
            with self._lock:
                if self._inflight.get(session_id) is request:
                    request.followed_session_id = held.session_id
                    request.followed_attempt = held.attempt
            return
        claim = self._mark_pending_in_store(
            worker_store,
            current,
            request.target_keys,
        )
        if isinstance(claim, _ClaimHeld):
            with self._lock:
                if self._inflight.get(session_id) is request:
                    request.followed_session_id = claim.session_id
                    request.followed_attempt = claim.attempt
            return
        if claim is None:
            self._complete_inflight(
                session_id,
                RecoveryResult(
                    session_id=session_id,
                    mode=request.mode,
                    state="skipped",
                    attempt=int(getattr(current, "recovery_attempts", 0) or 0),
                    error="recovery became ineligible while following another owner",
                ),
                expected=request,
            )
            return
        self._promote_claim_follower(
            session_id,
            request,
            current,
            claim,
            worker_store,
        )

    def _followed_ready_pid(self, record) -> int | None:
        state, state_pid, _detail = self._readiness_state(record)
        if state == "ready" and state_pid is not None and int(state_pid) > 1:
            return int(state_pid)
        try:
            pid = self._probe_ready(record)
        except (OSError, TmuxError):
            return None
        if pid is None or not self._payload_ready(record, pid):
            return None
        return pid

    def _foreign_claim(
        self,
        store,
        target_keys: tuple[str, ...],
    ) -> _ClaimHeld | None:
        connection = getattr(store, "conn", None)
        if connection is None or not target_keys:
            return None
        rows = connection.execute(
            f"""
            SELECT claims.session_id,
                   claims.owner_id,
                   claims.expires_at,
                   sessions.recovery_attempts
            FROM recovery_claims AS claims
            LEFT JOIN sessions ON sessions.id = claims.session_id
            WHERE claims.target_key IN ({", ".join("?" for _ in target_keys)})
            """,
            list(target_keys),
        ).fetchall()
        now = float(self.wall_clock())
        held: list[_ClaimHeld] = []
        for row in rows:
            try:
                expires_at = float(row["expires_at"])
            except (TypeError, ValueError):
                continue
            owner_id = str(row["owner_id"])
            if (
                expires_at > now
                and owner_id != self.owner_id
                and self._claim_owner_alive(owner_id)
            ):
                held.append(
                    _ClaimHeld(
                        owner_id=owner_id,
                        expires_at=expires_at,
                        session_id=str(row["session_id"]),
                        attempt=int(row["recovery_attempts"] or 0),
                    )
                )
        return max(held, key=lambda item: item.expires_at) if held else None

    def _promote_claim_follower(
        self,
        session_id: str,
        request: _Request,
        record,
        claim: _Claim,
        worker_store,
    ) -> None:
        error: Exception | None = None
        with self._lock:
            if self._closed or self._inflight.get(session_id) is not request:
                error = _AdmissionUnavailable("recovery coordinator closed during claim handoff")
            else:
                request.record = record
                request.attempt = claim.attempt
                request.claim_token = claim.token
                request.target_keys = claim.target_keys
                request.following_claim = False
                for key in claim.target_keys:
                    self._inflight_keys.setdefault(key, session_id)
                try:
                    request.future = self._executor.submit(
                        self._run,
                        session_id,
                        claim.attempt,
                    )
                except Exception as exc:
                    error = exc
        if error is None:
            return
        try:
            self._persist_failure(
                worker_store,
                record,
                claim.attempt,
                str(error),
                claim_token=claim.token,
                target_keys=claim.target_keys,
            )
        except Exception:
            pass
        self._complete_inflight(
            session_id,
            RecoveryResult(
                session_id=session_id,
                mode=request.mode,
                state="failed",
                attempt=claim.attempt,
                error=str(error),
            ),
            expected=request,
        )

    def shutdown(self, *, wait: bool = False, timeout: float | None = None,
                 persist_cancelled: bool = True) -> bool:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("invalid recovery shutdown timeout")
        with self._lock:
            self._closed = True
            follower_thread = self._follower_thread
            requests = tuple(self._inflight.items())
            for _session_id, request in requests:
                request.cancelled.set()
        self._follower_wakeup.set()
        cancelled: list[tuple[str, _Request]] = []
        for session_id, request in requests:
            future = request.future
            if request.following_claim or future is None or future.cancel():
                cancelled.append((session_id, request))
        for session_id, request in cancelled:
            result = RecoveryResult(
                session_id=str(getattr(request.record, "id", "")),
                mode=request.mode,
                state="failed",
                attempt=request.attempt,
                error="recovery coordinator shut down before execution",
            )
            if request.claim_token and persist_cancelled:
                try:
                    self._persist_failure(
                        self.store,
                        request.record,
                        request.attempt,
                        result.error,
                        claim_token=request.claim_token,
                        target_keys=request.target_keys,
                    )
                except Exception:
                    pass
            self._complete_inflight(session_id, result, expected=request)
        self._executor.shutdown(wait=wait and timeout is None, cancel_futures=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        threads = tuple(self._executor._threads)
        if wait and deadline is not None:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
        if wait and follower_thread is not None and follower_thread is not threading.current_thread():
            follower_thread.join(timeout=(max(1.0, self.poll_interval * 4.0) if deadline is None
                                         else max(0, deadline - time.monotonic())))
        return not any(thread.is_alive() for thread in threads) and not (
            follower_thread is not None and follower_thread.is_alive())

    close = shutdown

    @staticmethod
    def _begin_store_write(store):
        connection = getattr(store, "conn", None)
        if connection is None:
            raise _AdmissionUnavailable("session store has no atomic connection")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _AdmissionUnavailable(
                f"session store admission transaction unavailable: {exc}"
            ) from exc
        return connection

    @staticmethod
    def _rollback_store_write(connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass

    @staticmethod
    def _claim_table(connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_claims (
                target_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                claim_token TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )

    @staticmethod
    def _process_start_token(pid: int) -> str | None:
        try:
            payload = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        closing = payload.rfind(")")
        if closing < 0:
            return None
        fields = payload[closing + 1 :].split()
        return fields[19] if len(fields) > 19 else None

    @staticmethod
    def _boot_id() -> str | None:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip() or None
        except (OSError, UnicodeError):
            return None

    @classmethod
    def _new_owner_id(cls) -> str:
        pid = os.getpid()
        start = cls._process_start_token(pid) or "unknown"
        boot = cls._boot_id() or "unknown"
        return f"{socket.gethostname()}:{pid}:{start}:{boot}:{uuid4().hex}"

    @classmethod
    def _claim_owner_alive(cls, owner_id: object) -> bool:
        parts = str(owner_id or "").split(":")
        if len(parts) not in {3, 5} or parts[0] != socket.gethostname():
            return True
        try:
            pid = int(parts[1])
        except (TypeError, ValueError):
            return True
        if pid <= 1:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (OSError, PermissionError):
            return True
        if len(parts) == 5:
            expected_start, expected_boot = parts[2], parts[3]
            observed_start = cls._process_start_token(pid)
            observed_boot = cls._boot_id()
            if (
                expected_start != "unknown"
                and observed_start is not None
                and observed_start != expected_start
            ):
                return False
            if (
                expected_boot != "unknown"
                and observed_boot is not None
                and observed_boot != expected_boot
            ):
                return False
        return True

    @staticmethod
    def _writer_lock_root(store) -> Path | None:
        configured = str(
            os.environ.get("TERMDECK_TMUX_RUNTIME", "")
            or os.environ.get("TMUX_TMPDIR", "")
        ).strip()
        if configured:
            root = Path(configured).expanduser()
            if not root.is_absolute():
                raise _AdmissionUnavailable(
                    "tmux recovery writer lock root must be absolute"
                )
            return Path(os.path.abspath(os.fspath(root))) / "recovery-writer-locks"
        path = getattr(store, "path", None)
        if path is None or Path(path) == Path(":memory:"):
            return None
        database = Path(path).expanduser()
        if not database.is_absolute():
            database = Path.cwd() / database
        return Path(os.path.abspath(os.fspath(database))).parent / "recovery-writer-locks"

    @contextmanager
    def _writer_scope(self, store, request: _Request, *, timeout: float | None = None):
        """Hold crash-released locks for only this request's target identities."""

        root = self._writer_lock_root(store)
        if root is None or not request.target_keys:
            yield
            return
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            metadata = root.lstat()
            if (
                root.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise _AdmissionUnavailable(
                    "recovery writer lock root has unsafe ownership or mode"
                )
        except _AdmissionUnavailable:
            raise
        except OSError as exc:
            raise _AdmissionUnavailable(
                f"recovery writer lock root is unavailable: {exc}"
            ) from exc

        locks: list[int] = []
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            for target_key in sorted(request.target_keys):
                name = hashlib.sha256(
                    target_key.encode("utf-8", "surrogatepass")
                ).hexdigest()
                path = root / f"{name}.lock"
                descriptor = os.open(
                    path,
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    metadata = os.fstat(descriptor)
                    path_metadata = path.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_nlink != 1
                        or (metadata.st_dev, metadata.st_ino)
                        != (path_metadata.st_dev, path_metadata.st_ino)
                    ):
                        raise _AdmissionUnavailable(
                            "recovery writer lock has unsafe ownership or mode"
                        )
                    while True:
                        self._raise_if_cancelled(request)
                        try:
                            fcntl.flock(
                                descriptor,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                            break
                        except BlockingIOError:
                            if deadline is not None and time.monotonic() >= deadline:
                                raise _AdmissionUnavailable("recovery target writer is busy")
                            request.cancelled.wait(min(self.poll_interval, 0.05))
                    locks.append(descriptor)
                    descriptor = -1
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            yield
        finally:
            for descriptor in reversed(locks):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @contextmanager
    def metadata_writer_guard(self, records, *, timeout: float = 1.0):
        """Share GTK/headless target locks for an explicit metadata-only repair."""
        records = list(records)
        if not records or not 0 < timeout <= 60:
            raise _AdmissionUnavailable("metadata writer selection is invalid")
        request = _Request(record=records[0], mode="metadata-repair", initial_grid=None,
                           listeners=[], target_keys=tuple(sorted({
                               key for record in records for key in self._single_flight_keys(record)
                           })))
        with self._writer_scope(self.store, request, timeout=timeout):
            yield

    def assert_metadata_quiescent(self, records) -> None:
        """Reject live or unverifiable owners, even if their lease has expired."""
        connection = getattr(self.store, "conn", None)
        if connection is None:
            raise _AdmissionUnavailable("metadata writer has no durable store")
        keys = sorted({key for record in records for key in self._single_flight_keys(record)})
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_claims'"
        ).fetchone()
        if not exists or not keys:
            return
        owners = connection.execute(
            f"SELECT owner_id FROM recovery_claims WHERE target_key IN ({','.join('?' for _ in keys)})",
            keys,
        ).fetchall()
        if any(self._claim_owner_alive(row["owner_id"]) for row in owners):
            raise _AdmissionUnavailable("recovery target has an active or unverifiable owner")

    @staticmethod
    def _target_key(kind: str, value: object) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        digest = hashlib.sha256(
            f"{kind}\0{normalized}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        return f"{kind}:{digest}"

    @staticmethod
    def _resume_uuid(command: object) -> str | None:
        try:
            tokens = shlex.split(str(command or "").strip())
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
                    return str(UUID(candidate))
                except (AttributeError, TypeError, ValueError):
                    return None
        return None

    def _single_flight_keys(self, record) -> tuple[str, ...]:
        values: list[tuple[str, object]] = [("session", getattr(record, "id", ""))]
        for kind, field in (
            ("tmux", "tmux_name"),
            ("pane", "pane_target"),
        ):
            values.append((kind, getattr(record, field, "")))
        if self._is_codex_recovery(record):
            auth_scope = str(getattr(record, "auth_scope_id", "") or "").strip()
            for field in ("recovery_command", "command"):
                command = str(getattr(record, field, "") or "").strip()
                if not command:
                    continue
                if resume_id := self._resume_uuid(command):
                    target = f"{auth_scope}\0{resume_id}" if auth_scope else resume_id
                    values.append(("auth-resume" if auth_scope else "resume", target))
                    break
        keys = {
            key
            for kind, value in values
            if (key := self._target_key(kind, value)) is not None
        }
        return tuple(sorted(keys))

    def _mark_pending(
        self,
        record,
        target_keys: tuple[str, ...],
    ) -> _Claim | _ClaimHeld | None:
        return self._mark_pending_in_store(self.store, record, target_keys)

    def _mark_pending_in_store(
        self,
        store,
        record,
        target_keys: tuple[str, ...],
    ) -> _Claim | _ClaimHeld | None:
        if store is None:
            if not startup_recovery_allowed(record):
                return None
            return _Claim(
                attempt=int(getattr(record, "recovery_attempts", 0) or 0) + 1,
                token=uuid4().hex,
                target_keys=target_keys,
            )
        connection = self._begin_store_write(store)
        try:
            self._claim_table(connection)
            current = store.get(str(getattr(record, "id")))
            if current is None or not startup_recovery_allowed(current):
                self._rollback_store_write(connection)
                return None
            now = float(self.wall_clock())
            for key in target_keys:
                existing = connection.execute(
                    """
                    SELECT session_id, owner_id, expires_at
                    FROM recovery_claims
                    WHERE target_key = ?
                    """,
                    (key,),
                ).fetchone()
                if existing is None:
                    continue
                try:
                    expires_at = float(existing["expires_at"])
                except (TypeError, ValueError):
                    expires_at = 0.0
                same_owner = (
                    str(existing["owner_id"]) == self.owner_id
                    and str(existing["session_id"]) == str(getattr(record, "id"))
                )
                owner_id = str(existing["owner_id"])
                if (
                    expires_at > now
                    and not same_owner
                    and self._claim_owner_alive(owner_id)
                ):
                    held_session_id = str(existing["session_id"])
                    held_record = store.get(held_session_id)
                    self._rollback_store_write(connection)
                    return _ClaimHeld(
                        owner_id=owner_id,
                        expires_at=expires_at,
                        session_id=held_session_id,
                        attempt=int(
                            getattr(held_record, "recovery_attempts", 0) or 0
                        ),
                    )
            attempt = int(getattr(current, "recovery_attempts", 0) or 0) + 1
            token = uuid4().hex
            expires_at = now + self.claim_ttl
            session_id = str(getattr(record, "id"))
            for key in target_keys:
                connection.execute(
                    """
                    INSERT INTO recovery_claims (
                        target_key, session_id, owner_id, claim_token, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(target_key) DO UPDATE SET
                        session_id = excluded.session_id,
                        owner_id = excluded.owner_id,
                        claim_token = excluded.claim_token,
                        expires_at = excluded.expires_at
                    """,
                    (key, session_id, self.owner_id, token, expires_at),
                )
            updated = connection.execute(
                """
                UPDATE sessions
                SET status = 'recovery-pending',
                    last_pid = NULL,
                    recovery_attempts = ?,
                    recovery_last_attempt_at = ?,
                    recovery_last_error = NULL,
                    recovery_intent = ?
                WHERE id = ?
                """,
                (
                    attempt,
                    utc_now(),
                    RECOVERY_INTENT_ACTIVE,
                    session_id,
                ),
            )
            if updated.rowcount != 1:
                self._rollback_store_write(connection)
                return None
            connection.commit()
            return _Claim(attempt=attempt, token=token, target_keys=target_keys)
        except Exception:
            self._rollback_store_write(connection)
            raise

    def _read_admission_record(self, fallback):
        if self.store is None:
            return fallback
        try:
            return self.store.get(str(getattr(fallback, "id")))
        except Exception as exc:
            raise _AdmissionUnavailable(
                f"session state could not be read: {exc}"
            ) from exc

    def _worker_store(self):
        if self.store_factory is not None:
            return self.store_factory(), True
        if self.store is None:
            return None, False
        path = getattr(self.store, "path", None)
        if path is None:
            return self.store, False
        try:
            return SessionStore(
                path,
                auth_root=getattr(self.store, "auth_root", None),
                account_source=getattr(self.store, "account_source", None),
                provision_auth_scopes=bool(
                    getattr(self.store, "provision_auth_scopes", False)
                ),
                ), True
        except Exception:
            return None, False

    @staticmethod
    def _close_store(store, owned: bool) -> None:
        if owned and store is not None:
            try:
                store.close()
            except Exception:
                pass

    @staticmethod
    def _raise_if_cancelled(request: _Request) -> None:
        if request.claim_lost.is_set():
            raise _AdmissionUnavailable("recovery claim was lost during execution")
        if request.cancelled.is_set():
            raise _AdmissionUnavailable(
                "recovery coordinator shut down before completion"
            )

    def _claim_database_path(self) -> Path | None:
        path = getattr(self.store, "path", None)
        if path is None or str(path) == ":memory:":
            return None
        database = Path(path).expanduser()
        if not database.is_absolute():
            database = Path.cwd() / database
        return Path(os.path.abspath(os.fspath(database)))

    def _heartbeat_claim_once(self, request: _Request) -> None:
        database = self._claim_database_path()
        if database is None or not request.claim_token:
            return
        connection = sqlite3.connect(
            database,
            timeout=max(0.1, min(5.0, self.claim_ttl / 4.0)),
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ", ".join("?" for _ in request.target_keys)
            expires_at = float(self.wall_clock()) + self.claim_ttl
            updated = connection.execute(
                f"""
                UPDATE recovery_claims
                SET expires_at = ?
                WHERE owner_id = ?
                  AND claim_token = ?
                  AND session_id = ?
                  AND target_key IN ({placeholders})
                """,
                [
                    expires_at,
                    self.owner_id,
                    request.claim_token,
                    str(getattr(request.record, "id")),
                    *request.target_keys,
                ],
            )
            if updated.rowcount != len(request.target_keys):
                connection.rollback()
                raise _AdmissionUnavailable("recovery claim was lost")
            connection.commit()
            request.next_claim_renewal = self.clock() + max(
                0.1,
                self.claim_ttl / 3.0,
            )
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _start_claim_heartbeat(
        self,
        request: _Request,
        stop: threading.Event,
    ) -> threading.Thread | None:
        if self._claim_database_path() is None or not request.claim_token:
            return None
        interval = max(0.1, self.claim_ttl / 3.0)

        def heartbeat() -> None:
            while not stop.wait(interval):
                if request.cancelled.is_set():
                    return
                try:
                    self._heartbeat_claim_once(request)
                except Exception:
                    request.claim_lost.set()
                    request.cancelled.set()
                    return

        thread = threading.Thread(
            target=heartbeat,
            name="termdeck-recovery-claim-heartbeat",
            daemon=True,
        )
        thread.start()
        return thread

    def _stop_claim_heartbeat(
        self,
        stop: threading.Event,
        thread: threading.Thread | None,
    ) -> None:
        stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, min(6.0, self.claim_ttl / 3.0 + 1.0)))

    def _run(self, session_id: str, attempt: int) -> None:
        with self._lock:
            request = self._inflight.get(session_id)
        if request is None:
            return
        record = request.record
        worker_store = None
        owned_store = False
        current = record
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            worker_store, owned_store = self._worker_store()
            if self.store is not None and worker_store is None:
                raise _AdmissionUnavailable("worker session store could not be opened")
            self._raise_if_cancelled(request)
            self._renew_claim(worker_store, current, request, force=True)
            heartbeat_thread = self._start_claim_heartbeat(request, heartbeat_stop)
            with self._writer_scope(worker_store or self.store, request):
                self._raise_if_cancelled(request)
                self._renew_claim(worker_store, current, request, force=True)
                current = self._read_record_from(worker_store, record)
                if current is None or not startup_recovery_allowed(current):
                    self._stop_claim_heartbeat(heartbeat_stop, heartbeat_thread)
                    heartbeat_thread = None
                    self._release_claim(worker_store, request)
                    result = RecoveryResult(
                        session_id=session_id,
                        mode=request.mode,
                        state="skipped",
                        attempt=attempt,
                        error="recovery was made inactive before execution",
                    )
                else:
                    current = self._maybe_rebind_auth_scope(
                        current,
                        request,
                        worker_store,
                    )
                    pid, created, outcome = self._recover_target(
                        current,
                        request,
                        worker_store,
                    )
                    self._stop_claim_heartbeat(heartbeat_stop, heartbeat_thread)
                    heartbeat_thread = None
                    self._raise_if_cancelled(request)
                    self._renew_claim(worker_store, current, request, force=True)
                    claimed, current = self._persist_ready_if_eligible(
                        worker_store,
                        current,
                        pid,
                        outcome=outcome,
                        claim_token=request.claim_token,
                        target_keys=request.target_keys,
                    )
                    if not claimed:
                        result = RecoveryResult(
                            session_id=session_id,
                            mode=request.mode,
                            state="skipped",
                            attempt=attempt,
                            error="recovery was made inactive before completion",
                        )
                    else:
                        result = RecoveryResult(
                            session_id=session_id,
                            mode=request.mode,
                            state="ready",
                            pid=pid,
                            created=created,
                            attempt=attempt,
                            action=getattr(outcome, "action", None),
                            command=getattr(outcome, "command", None),
                            cwd=getattr(outcome, "cwd", None),
                            clear_recovery=bool(
                                getattr(outcome, "clear_recovery", False)
                            ),
                        )
        except Exception as exc:
            self._stop_claim_heartbeat(heartbeat_stop, heartbeat_thread)
            heartbeat_thread = None
            error = str(exc) or exc.__class__.__name__
            try:
                self._persist_failure(
                    worker_store,
                    current,
                    attempt,
                    error,
                    claim_token=request.claim_token,
                    target_keys=request.target_keys,
                )
            except Exception:
                pass
            result = RecoveryResult(
                session_id=session_id,
                mode=request.mode,
                state="failed",
                attempt=attempt,
                error=error,
            )
        finally:
            self._stop_claim_heartbeat(heartbeat_stop, heartbeat_thread)
            self._close_store(worker_store, owned_store)
        with self._lock:
            completed = self._inflight.pop(session_id, None)
            if completed is not None:
                for key in completed.target_keys:
                    if self._inflight_keys.get(key) == session_id:
                        self._inflight_keys.pop(key, None)
        if completed is None:
            return
        for listener in completed.listeners:
            self._deliver(result, listener)

    @staticmethod
    def _read_record_from(store, fallback):
        if store is None:
            return fallback
        return store.get(str(getattr(fallback, "id")))

    def _auth_scope_rebind_proof(
        self,
        record,
        *,
        quiescence_observation: object = _NO_QUIESCENCE_OBSERVATION,
    ) -> tuple[bool, AuthScopeRebindProof]:
        """Collect independent missing/dead/quiescent evidence for rebinding."""

        exists = getattr(self.backend, "session_exists", None)
        if not callable(exists):
            raise TmuxError("cannot prove the Codex recovery target is missing")
        try:
            if bool(exists(record)):
                return False, AuthScopeRebindProof()
        except (OSError, TmuxError) as exc:
            raise TmuxError(
                f"cannot prove the Codex recovery target is missing: {exc}"
            ) from exc

        dead = False
        authoritative = getattr(self.backend, "authoritative_live_pane_pid", None)
        if callable(authoritative):
            try:
                dead = authoritative(record) is None
            except (OSError, TmuxError):
                dead = False

        quiescent = self._reported_quiescent(quiescence_observation)
        if quiescence_observation is _NO_QUIESCENCE_OBSERVATION:
            for name in (
                "auth_scope_writer_quiescent",
                "rollout_lifecycle_quiescent",
                "auth_scope_quiescent",
                "recovery_quiescent",
                "quiescent_auth_scope",
                "scope_quiescent",
            ):
                probe = getattr(self.backend, name, None)
                if not callable(probe):
                    continue
                try:
                    value = probe(record)
                except (OSError, TmuxError):
                    continue
                quiescent = self._reported_quiescent(value)
                break
        return True, AuthScopeRebindProof(
            missing=True,
            dead=dead,
            quiescent=quiescent,
        )

    @staticmethod
    def _reported_quiescent(value: object) -> bool:
        if isinstance(value, dict):
            return value.get("quiescent") is True
        return value is True

    @staticmethod
    def _private_owned_directory(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return (
            not path.is_symlink()
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )

    def _record_uses_exact_managed_auth_scope(self, record, store) -> bool:
        """Recognize only the exact private scope already owned by the store."""

        if store is None:
            return False
        scope_id = str(getattr(record, "auth_scope_id", "") or "").strip()
        raw_home = str(getattr(record, "codex_home", "") or "").strip()
        raw_root = getattr(store, "auth_root", None)
        if (
            not scope_id.startswith("auth-")
            or Path(scope_id).name != scope_id
            or not raw_home
            or raw_root is None
        ):
            return False
        home = Path(raw_home).expanduser()
        root = Path(raw_root).expanduser()
        if not home.is_absolute() or not root.is_absolute():
            return False
        exact_home = Path(os.path.abspath(os.fspath(home)))
        exact_root = Path(os.path.abspath(os.fspath(root)))
        expected_home = exact_root / scope_id
        return (
            exact_home == expected_home
            and self._private_owned_directory(exact_root)
            and self._private_owned_directory(expected_home)
        )

    def _record_uses_saved_auth_scope(self, record, store) -> bool:
        """Preserve validated historical direct Bash homes without rebinding."""
        if self._record_uses_exact_managed_auth_scope(record, store):
            return True
        from .codex_config_scope import CodexConfigScopeError, validate_existing_private_bash_scope
        try:
            recipe = recipe_from_record(record)
            direct_bash = getattr(self.backend, "_saved_direct_bash_command", None)
            if (recipe is None or recipe.origin not in {"launch", "legacy-launch"}
                    or not callable(direct_bash)
                    or not direct_bash(record, recipe.command)
                    or not getattr(record, "codex_home", None)
                    or not getattr(record, "auth_scope_id", None)):
                return False
            validate_existing_private_bash_scope(record.codex_home, auth_scope_id=record.auth_scope_id)
            return True
        except (SessionRecipeError, CodexConfigScopeError, OSError, ValueError):
            return False

    def _rebind_legacy_auth_scope(
        self,
        record,
        request: _Request,
        worker_store,
        *,
        quiescence_observation: object = _NO_QUIESCENCE_OBSERVATION,
    ):
        missing, proof = self._auth_scope_rebind_proof(
            record,
            quiescence_observation=quiescence_observation,
        )
        if not missing:
            return record
        rebind_store = worker_store or self.store
        result = rebind_auth_scope(
            record,
            rebind_store,
            proof=proof,
            claim_token=request.claim_token,
            claim_owner=self.owner_id,
            claim_target_keys=request.target_keys,
            expected_old_home=getattr(record, "codex_home", None),
            canonical_home=getattr(rebind_store, "account_source", None),
            auth_root=getattr(rebind_store, "auth_root", None),
        )
        if not result.ok:
            raise TmuxError(f"Codex auth-scope rebind deferred: {result.detail}")
        refreshed = self._read_record_from(worker_store, record)
        if refreshed is None:
            raise TmuxError("session disappeared after Codex auth-scope rebind")
        return refreshed

    def _maybe_rebind_auth_scope(self, record, request: _Request, worker_store):
        """Move only an unbound exact Codex resume onto managed SSD state."""

        managed_stores = (worker_store, self.store)
        if not self.allow_auth_scope_rebind and (
            getattr(record, "codex_home", None)
            or getattr(record, "auth_scope_id", None)
        ) and not any(
            candidate is not None
            and self._record_uses_saved_auth_scope(record, candidate)
            for candidate in managed_stores
        ):
            raise TmuxError("saved private Codex auth scope is unavailable; automatic rebind is disabled")
        if not getattr(record, "codex_home", None):
            return record
        if not getattr(record, "auth_scope_id", None):
            return record
        if not self._is_codex_recovery(record):
            return record
        if any(
            candidate is not None
            and self._record_uses_saved_auth_scope(record, candidate)
            for candidate in managed_stores
        ):
            return record
        quiescence = getattr(self.backend, "auth_scope_writer_quiescence", None)
        if callable(quiescence):
            with quiescence(record) as observation:
                return self._rebind_legacy_auth_scope(
                    record,
                    request,
                    worker_store,
                    quiescence_observation=observation,
                )
        return self._rebind_legacy_auth_scope(record, request, worker_store)

    def _recover_target(
        self,
        record,
        request: _Request,
        worker_store,
    ) -> tuple[int, bool, object | None]:
        if request.mode == "payload":
            recover = getattr(self.backend, "recover_dead_payload", None)
            exists = getattr(self.backend, "session_exists", None)
            target_exists = bool(exists(record)) if callable(exists) else True
            if callable(recover) and target_exists:
                outcome = recover(record)
                pid = getattr(outcome, "pid", None)
                if pid is None and isinstance(outcome, int):
                    pid = outcome
                if pid is not None and int(pid) > 1:
                    pid = int(pid)
                    action = str(getattr(outcome, "action", "")).casefold()
                    if action == "shell" or self._payload_ready(record, pid):
                        return (
                            pid,
                            bool(
                                getattr(outcome, "action", "")
                                not in {"alive", "repaired"}
                            ),
                            outcome,
                        )
                    ready_pid = self._await_ready(record, worker_store, request)
                    if ready_pid is not None:
                        return (
                            ready_pid,
                            bool(
                                getattr(outcome, "action", "")
                                not in {"alive", "repaired"}
                            ),
                            outcome,
                        )
                if str(getattr(outcome, "action", "")).casefold() in {"alive", "repaired"}:
                    ready_pid = self._await_ready(record, worker_store, request)
                    if ready_pid is not None:
                        return ready_pid, False, outcome
                raise TmuxError("payload recovery did not produce a live target")
        ready_pid = self._probe_ready(record)
        if ready_pid is not None:
            if self._payload_ready(record, ready_pid):
                return ready_pid, False, None
            ready_pid = self._await_ready(record, worker_store, request)
            if ready_pid is not None:
                return ready_pid, False, None
            exists = getattr(self.backend, "session_exists", None)
            if not callable(exists) or bool(exists(record)):
                raise TmuxError("existing recovery target did not become ready")
        create_at_grid = getattr(self.backend, "create_or_resume_at_grid", None)
        if request.initial_grid is not None and callable(create_at_grid):
            columns, rows = request.initial_grid
            create_at_grid(record, columns=columns, rows=rows)
        else:
            create = getattr(self.backend, "create_or_resume", None)
            if not callable(create):
                raise TmuxError("backend cannot create a recovery target")
            create(record)
        ready_pid = self._await_ready(record, worker_store, request)
        if ready_pid is None:
            raise TmuxError("recovery target did not become ready")
        return ready_pid, True, None

    def _probe_ready(self, record) -> int | None:
        exists = getattr(self.backend, "session_exists", None)
        if callable(exists) and not bool(exists(record)):
            return None
        authoritative = getattr(self.backend, "authoritative_live_pane_pid", None)
        if not callable(authoritative):
            return None
        pid = authoritative(record)
        if pid is None or int(pid) <= 1:
            return None
        return int(pid)

    def _known_live_ready(self, record, pid: int) -> bool:
        """Require authoritative live evidence for a known pane PID."""

        state, state_pid, _ = self._readiness_state(record)
        if state == "ready" and (
            state_pid is None or int(state_pid) == int(pid)
        ):
            return True
        authoritative = getattr(self.backend, "authoritative_live_pane_pid", None)
        if not callable(authoritative):
            return False
        try:
            observed = authoritative(record)
        except (OSError, TmuxError):
            return False
        if observed is None or int(observed) != int(pid):
            return False
        return self._payload_ready(record, int(pid))

    @staticmethod
    def _is_codex_recovery(record) -> bool:
        values = (
            getattr(record, "command", ""),
            getattr(record, "recovery_command", ""),
        )
        for value in values:
            raw = str(value or "").strip()
            if not raw:
                continue
            try:
                tokens = shlex.split(raw)
            except ValueError:
                tokens = raw.split()
            for token in tokens:
                name = str(token).rsplit("/", 1)[-1].casefold()
                if name == "codex" or name.startswith("codex-"):
                    return True
        return False

    def _warp_drives_ready(self, record) -> bool:
        for name in (
            "warp_drives_ready",
            "workspace_cell_ready",
            "workspace_ready",
            "recovery_ready",
        ):
            probe = getattr(self.backend, name, None)
            if not callable(probe):
                continue
            try:
                if probe(record) is True:
                    return True
            except (OSError, TmuxError):
                continue
        return False

    def _payload_ready(self, record, pid: int) -> bool:
        if not self._is_codex_recovery(record):
            return True
        if self._warp_drives_ready(record):
            return True
        try:
            return process_tree_contains_codex(pid) is True
        except (OSError, PermissionError):
            return False

    def _await_ready(
        self,
        record,
        worker_store=None,
        request: _Request | None = None,
    ) -> int | None:
        deadline = self.clock() + self.readiness_timeout
        idle_failure = None
        idle_since = None
        seen_target = False
        last_detail = ""
        while True:
            if request is not None:
                self._raise_if_cancelled(request)
            else:
                with self._lock:
                    if self._closed:
                        raise TmuxError("recovery coordinator closed before readiness")
            self._renew_claim(worker_store, record, request)
            state, state_pid, state_detail = self._readiness_state(record)
            if state_detail:
                last_detail = state_detail
            if state == "failed":
                transient_idle = (self.readiness_failure_grace > 0
                    and last_detail == "managed Codex launcher exited to an idle shell")
                marker = (state_pid, last_detail)
                if not transient_idle:
                    raise TmuxError(last_detail or "managed recovery launcher reported failure")
                if idle_failure != marker:
                    idle_failure, idle_since = marker, self.clock()
                if self.clock() - idle_since >= self.readiness_failure_grace:
                    raise TmuxError(last_detail)
            else:
                idle_failure, idle_since = None, None
            if state == "ready" and state_pid is not None and int(state_pid) > 1:
                return int(state_pid)
            try:
                pid = self._probe_ready(record)
            except (OSError, TmuxError):
                pid = None
            if pid is not None and self._payload_ready(record, pid):
                return pid
            present = self._session_present(record)
            if present is True:
                seen_target = True
            elif present is False and seen_target:
                raise TmuxError(
                    last_detail or "managed recovery target exited before readiness"
                )
            if self.clock() >= deadline:
                return None
            self.sleeper(self.poll_interval)

    def _readiness_state(self, record) -> tuple[str, int | None, str]:
        """Read an optional managed-launcher lifecycle state.

        The existing backend can prove readiness from tmux and process state.
        The SSD workspace launcher can additionally expose a durable lifecycle
        probe; accepting both keeps the coordinator compatible while making a
        pending import distinct from a failed launch.
        """

        for name in (
            "recovery_readiness",
            "workspace_recovery_status",
            "managed_recovery_status",
            "recovery_status",
        ):
            if name == "recovery_readiness" and not self._is_codex_recovery(record):
                continue
            probe = getattr(self.backend, name, None)
            if not callable(probe):
                continue
            try:
                value = probe(record)
            except (AttributeError, NotImplementedError):
                continue
            except (OSError, TmuxError):
                return "pending", None, "managed recovery status is temporarily unavailable"
            return self._normalise_readiness(value)
        return "unknown", None, ""

    @staticmethod
    def _normalise_readiness(value) -> tuple[str, int | None, str]:
        if isinstance(value, bool):
            return ("ready" if value else "pending"), None, ""
        if isinstance(value, str):
            state = value.strip().casefold().replace("_", "-")
            if state in {"ready", "running", "complete", "completed"}:
                return "ready", None, ""
            if state in {"failed", "error", "terminal-failed", "cancelled"}:
                return "failed", None, value
            return "pending", None, value
        if isinstance(value, dict):
            raw_state = value.get("state", value.get("status", "pending"))
            state, _, detail = RecoveryCoordinator._normalise_readiness(raw_state)
            raw_pid = value.get("pid", value.get("pane_pid"))
            try:
                pid = int(raw_pid) if raw_pid is not None else None
            except (TypeError, ValueError):
                pid = None
            return state, pid, str(value.get("detail", value.get("error", detail)) or "")
        raw_state = getattr(value, "state", getattr(value, "status", "pending"))
        state, _, detail = RecoveryCoordinator._normalise_readiness(raw_state)
        raw_pid = getattr(value, "pid", getattr(value, "pane_pid", None))
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        return state, pid, str(
            getattr(value, "detail", getattr(value, "error", detail)) or ""
        )

    def _session_present(self, record) -> bool | None:
        exists = getattr(self.backend, "session_exists", None)
        if not callable(exists):
            return None
        try:
            return bool(exists(record))
        except (OSError, TmuxError):
            return None

    def _renew_claim(
        self,
        store,
        record,
        request: _Request | None,
        *,
        force: bool = False,
    ) -> None:
        if store is None or request is None or not request.claim_token:
            return
        now = self.clock()
        if not force and now < request.next_claim_renewal:
            return
        connection = self._begin_store_write(store)
        try:
            self._claim_table(connection)
            expires_at = float(self.wall_clock()) + self.claim_ttl
            placeholders = ", ".join("?" for _ in request.target_keys)
            values: list[object] = [
                expires_at,
                self.owner_id,
                request.claim_token,
                str(getattr(record, "id")),
                *request.target_keys,
            ]
            updated = connection.execute(
                f"""
                UPDATE recovery_claims
                SET expires_at = ?
                WHERE owner_id = ?
                  AND claim_token = ?
                  AND session_id = ?
                  AND target_key IN ({placeholders})
                """,
                values,
            )
            if updated.rowcount != len(request.target_keys):
                self._rollback_store_write(connection)
                raise _AdmissionUnavailable("recovery claim was lost")
            connection.commit()
            request.next_claim_renewal = now + max(0.1, self.claim_ttl / 3.0)
        except Exception:
            self._rollback_store_write(connection)
            raise

    def _release_claim(self, store, request: _Request | None) -> None:
        if store is None or request is None or not request.claim_token:
            return
        connection = self._begin_store_write(store)
        try:
            self._claim_table(connection)
            placeholders = ", ".join("?" for _ in request.target_keys)
            connection.execute(
                f"""
                DELETE FROM recovery_claims
                WHERE owner_id = ?
                  AND claim_token = ?
                  AND session_id = ?
                  AND target_key IN ({placeholders})
                """,
                [
                    self.owner_id,
                    request.claim_token,
                    str(getattr(request.record, "id")),
                    *request.target_keys,
                ],
            )
            connection.commit()
        except Exception:
            self._rollback_store_write(connection)
            raise

    def _persist_ready_if_eligible(
        self,
        store,
        record,
        pid: int,
        *,
        outcome: object | None = None,
        claim_token: str | None = None,
        target_keys: tuple[str, ...] = (),
    ) -> tuple[bool, object | None]:
        if store is None:
            return startup_recovery_allowed(record), record
        connection = self._begin_store_write(store)
        try:
            self._claim_table(connection)
            current = store.get(str(getattr(record, "id")))
            if current is None or not startup_recovery_allowed(current):
                if claim_token:
                    self._delete_claims(connection, current or record, claim_token, target_keys)
                    connection.commit()
                    return False, current
                self._rollback_store_write(connection)
                return False, current
            if target_keys:
                claim_rows = connection.execute(
                    f"""
                    SELECT target_key, owner_id, claim_token, expires_at
                    FROM recovery_claims
                    WHERE target_key IN ({", ".join("?" for _ in target_keys)})
                    """,
                    list(target_keys),
                ).fetchall()
                now = float(self.wall_clock())
                if claim_token:
                    if len(claim_rows) != len(target_keys) or any(
                        str(row["owner_id"]) != self.owner_id
                        or str(row["claim_token"]) != claim_token
                        or float(row["expires_at"]) <= now
                        for row in claim_rows
                    ):
                        self._rollback_store_write(connection)
                        return False, current
                elif any(
                    float(row["expires_at"]) > now
                    and str(row["owner_id"]) != self.owner_id
                    for row in claim_rows
                ):
                    self._rollback_store_write(connection)
                    return False, current
            status = str(getattr(current, "status", "")).casefold()
            if status in {"missing", "recovery-pending", "start-error"}:
                status = "running"
            clear_recovery = bool(getattr(outcome, "clear_recovery", False))
            if clear_recovery:
                command = str(getattr(outcome, "command", "") or "").strip()
                cwd = str(getattr(outcome, "cwd", "") or "").strip()
                if not command or not cwd:
                    raise _AdmissionUnavailable(
                        "shell bridge did not provide a durable command and directory"
                    )
                recipe, recipe_sha256, restart_policy = record_recipe_fields(
                    command=command,
                    cwd=cwd,
                    restart_policy="on-failure",
                    origin="launch",
                )
                updated = connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?,
                        last_pid = ?,
                        recovery_last_error = NULL,
                        last_opened_at = ?,
                        command = ?,
                        cwd = ?,
                        recovery_command = NULL,
                        recovery_cwd = NULL,
                        recovery_recipe = ?,
                        recovery_recipe_sha256 = ?,
                        restart_policy = ?,
                        auth_restart_required = 0
                    WHERE id = ?
                    """,
                    (
                        status or "running",
                        pid,
                        utc_now(),
                        command,
                        cwd,
                        recipe,
                        recipe_sha256,
                        restart_policy,
                        str(getattr(record, "id")),
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?,
                        last_pid = ?,
                        recovery_last_error = NULL,
                        last_opened_at = ?
                    WHERE id = ?
                    """,
                    (
                        status or "running",
                        pid,
                        utc_now(),
                        str(getattr(record, "id")),
                    ),
                )
            if updated.rowcount != 1:
                self._rollback_store_write(connection)
                return False, current
            if claim_token:
                self._delete_claims(connection, current, claim_token, target_keys)
            connection.commit()
            return True, store.get(str(getattr(record, "id")))
        except Exception:
            self._rollback_store_write(connection)
            raise

    def _delete_claims(
        self,
        connection,
        record,
        claim_token: str,
        target_keys: tuple[str, ...],
    ) -> None:
        if not target_keys:
            return
        placeholders = ", ".join("?" for _ in target_keys)
        connection.execute(
            f"""
            DELETE FROM recovery_claims
            WHERE owner_id = ?
              AND claim_token = ?
              AND session_id = ?
              AND target_key IN ({placeholders})
            """,
            [
                self.owner_id,
                claim_token,
                str(getattr(record, "id")),
                *target_keys,
            ],
        )

    def _persist_failure(
        self,
        store,
        record,
        attempt: int,
        error: str,
        *,
        claim_token: str | None = None,
        target_keys: tuple[str, ...] = (),
    ) -> None:
        if store is None:
            return
        connection = self._begin_store_write(store)
        try:
            self._claim_table(connection)
            current = store.get(str(getattr(record, "id")))
            if current is None or not startup_recovery_allowed(current):
                if claim_token:
                    self._delete_claims(connection, current or record, claim_token, target_keys)
                    connection.commit()
                    return
                self._rollback_store_write(connection)
                return
            if claim_token and target_keys:
                claim_rows = connection.execute(
                    f"""
                    SELECT target_key, owner_id, claim_token, expires_at
                    FROM recovery_claims
                    WHERE target_key IN ({", ".join("?" for _ in target_keys)})
                    """,
                    list(target_keys),
                ).fetchall()
                if len(claim_rows) != len(target_keys) or any(
                    str(row["owner_id"]) != self.owner_id
                    or str(row["claim_token"]) != claim_token
                    or float(row["expires_at"]) <= float(self.wall_clock())
                    for row in claim_rows
                ):
                    self._rollback_store_write(connection)
                    return
            updated = connection.execute(
                """
                UPDATE sessions
                SET status = 'recovery-pending',
                    last_pid = NULL,
                    recovery_attempts = ?,
                    recovery_last_error = ?
                WHERE id = ?
                """,
                (
                    attempt,
                    error,
                    str(getattr(record, "id")),
                ),
            )
            if updated.rowcount != 1:
                self._rollback_store_write(connection)
                return
            if claim_token:
                self._delete_claims(connection, current, claim_token, target_keys)
            connection.commit()
        except Exception:
            self._rollback_store_write(connection)
            raise

    def _deliver(self, result: RecoveryResult, listener: _Listener) -> None:
        if listener.session_id and listener.session_id != result.session_id:
            result = replace(result, session_id=listener.session_id)
        callback = listener.on_ready if result.ready else listener.on_failed
        if callback is None:
            return

        def invoke() -> object:
            try:
                return callback(result)
            except Exception:
                return None

        if self.callback_executor is not None:
            try:
                self.callback_executor(invoke)
                return
            except Exception:
                pass
        invoke()


def recoverable_records(records: Iterable[object]) -> list[object]:
    """Return only rows permitted to enter the shared recovery queue."""

    return [record for record in records if startup_recovery_allowed(record)]


__all__ = [
    "RecoveryCoordinator",
    "RecoveryResult",
    "RecoverySubmission",
    "recoverable_records",
]
