"""Bounded, durable recovery for managed TermDeck sessions."""

from __future__ import annotations

import math
import os
import shlex
import sqlite3
import hashlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable, Iterable
from uuid import uuid4

from .auth_scope_rebind import AuthScopeRebindProof, rebind_auth_scope
from .models import RECOVERY_INTENT_ACTIVE
from .session_reconcile import startup_recovery_allowed
from .resources import process_tree_contains_codex
from .session_store import SessionStore, utc_now
from .tmux_backend import TmuxError


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


class _AdmissionUnavailable(RuntimeError):
    """The durable session state could not be inspected or claimed."""


@dataclass(frozen=True)
class _ClaimHeld:
    """A compatible recovery target is owned by another live coordinator."""

    owner_id: str
    expires_at: float


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
        self.owner_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._inflight: dict[str, _Request] = {}
        self._inflight_keys: dict[str, str] = {}
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
            existing = next(
                (
                    self._inflight.get(existing_id)
                    for key in target_keys
                    if (existing_id := self._inflight_keys.get(key)) is not None
                ),
                None,
            )
            if existing is not None:
                existing.listeners.append(listener)
                return RecoverySubmission(
                    session_id=session_id,
                    state="deduplicated",
                    accepted=True,
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
                    # A different process may currently own the same target.
                    # Do not let a live-looking pane race that durable launch.
                    return RecoverySubmission(
                        session_id=session_id,
                        state="deduplicated",
                        accepted=True,
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
                return RecoverySubmission(
                    session_id=session_id,
                    state="deduplicated",
                    accepted=True,
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

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        cancelled: list[_Request] = []
        if not wait:
            with self._lock:
                for session_id, request in tuple(self._inflight.items()):
                    future = request.future
                    if future is None or not future.cancelled():
                        continue
                    self._inflight.pop(session_id, None)
                    for key in request.target_keys:
                        if self._inflight_keys.get(key) == session_id:
                            self._inflight_keys.pop(key, None)
                    cancelled.append(request)
        for request in cancelled:
            result = RecoveryResult(
                session_id=str(getattr(request.record, "id", "")),
                mode=request.mode,
                state="failed",
                attempt=request.attempt,
                error="recovery coordinator shut down before execution",
            )
            for listener in request.listeners:
                self._deliver(result, listener)
            try:
                self._persist_failure(
                    self.store,
                    request.record,
                    request.attempt,
                    result.error or "recovery coordinator shut down",
                    claim_token=request.claim_token,
                    target_keys=request.target_keys,
                )
            except Exception:
                pass

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
    def _target_key(kind: str, value: object) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        digest = hashlib.sha256(
            f"{kind}\0{normalized}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        return f"{kind}:{digest}"

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
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    tokens = command.split()
                lowered = {str(token).casefold() for token in tokens}
                if "resume" in lowered or "--thread" in lowered:
                    target = f"{auth_scope}\0{command}" if auth_scope else command
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
        if self.store is None:
            if not startup_recovery_allowed(record):
                return None
            return _Claim(
                attempt=int(getattr(record, "recovery_attempts", 0) or 0) + 1,
                token=uuid4().hex,
                target_keys=target_keys,
            )
        connection = self._begin_store_write(self.store)
        try:
            self._claim_table(connection)
            current = self.store.get(str(getattr(record, "id")))
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
                if expires_at > now and not same_owner:
                    self._rollback_store_write(connection)
                    return _ClaimHeld(str(existing["owner_id"]), expires_at)
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

    def _run(self, session_id: str, attempt: int) -> None:
        with self._lock:
            request = self._inflight.get(session_id)
        if request is None:
            return
        record = request.record
        worker_store = None
        owned_store = False
        current = record
        try:
            worker_store, owned_store = self._worker_store()
            if self.store is not None and worker_store is None:
                raise _AdmissionUnavailable("worker session store could not be opened")
            current = self._read_record_from(worker_store, record)
            if current is None or not startup_recovery_allowed(current):
                self._release_claim(worker_store, request)
                result = RecoveryResult(
                    session_id=session_id,
                    mode=request.mode,
                    state="skipped",
                    attempt=attempt,
                    error="recovery was made inactive before execution",
                )
            else:
                self._renew_claim(worker_store, current, request, force=True)
                current = self._maybe_rebind_auth_scope(current, request, worker_store)
                pid, created, outcome = self._recover_target(
                    current,
                    request,
                    worker_store,
                )
                claimed, current = self._persist_ready_if_eligible(
                    worker_store,
                    current,
                    pid,
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

        quiescent = False
        for name in (
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
            if isinstance(value, dict):
                quiescent = value.get("quiescent") is True
            else:
                quiescent = value is True
            break
        if not quiescent:
            auth_exists = getattr(self.backend, "_auth_session_exists", None)
            if callable(auth_exists):
                try:
                    quiescent = not bool(auth_exists(record))
                except (OSError, TmuxError):
                    quiescent = False
        return True, AuthScopeRebindProof(
            missing=True,
            dead=dead,
            quiescent=quiescent,
        )

    def _maybe_rebind_auth_scope(self, record, request: _Request, worker_store):
        """Move only an unbound exact Codex resume onto managed SSD state."""

        if not getattr(record, "codex_home", None):
            return record
        if not getattr(record, "auth_scope_id", None):
            return record
        if not self._is_codex_recovery(record):
            return record
        missing, proof = self._auth_scope_rebind_proof(record)
        if not missing:
            return record
        result = rebind_auth_scope(
            record,
            worker_store or self.store,
            proof=proof,
            claim_token=request.claim_token,
            claim_owner=self.owner_id,
            claim_target_keys=request.target_keys,
            expected_old_home=getattr(record, "codex_home", None),
            now=float(self.wall_clock()),
        )
        if not result.ok:
            raise TmuxError(f"Codex auth-scope rebind deferred: {result.detail}")
        refreshed = self._read_record_from(worker_store, record)
        if refreshed is None:
            raise TmuxError("session disappeared after Codex auth-scope rebind")
        return refreshed

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
        seen_target = False
        last_detail = ""
        while True:
            self._renew_claim(worker_store, record, request)
            state, state_pid, state_detail = self._readiness_state(record)
            if state_detail:
                last_detail = state_detail
            if state == "failed":
                raise TmuxError(
                    last_detail or "managed recovery launcher reported failure"
                )
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
            return True, current
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
