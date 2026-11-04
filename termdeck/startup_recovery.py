"""Restore saved terminals without requiring a desktop primary process.

The web server is a read-only tmux client. Its owning service runs this bounded
worker using GTK's durable recovery coordinator. Neither a socket inode nor a
saved numeric PID establishes pane liveness.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from .recovery_coordinator import RecoveryCoordinator
from .session_recipe import SessionRecipeError, recipe_from_record
from .session_reconcile import startup_recovery_allowed
from .session_store import SessionStore, default_db_path
from .storage_fabric import require_storage_fabric
from .tmux_backend import TmuxBackend

_ATTEMPT_SCHEMA = "termdeck.startup-recovery-attempts.v2"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class StartupRecoveryError(RuntimeError):
    """Admission failed before any terminal could be started."""


def _private_file(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise StartupRecoveryError("startup recovery file ownership is unsafe")
    return fd


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or path.resolve() != path):
        raise StartupRecoveryError("startup recovery directory ownership is unsafe")


@contextmanager
def startup_lock(root: Path):
    """Serialize backend workers; durable target claims also serialize GTK."""
    _private_directory(root)
    fd = _private_file(root / "startup-recovery.lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _process_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def _load_attempts(path: Path, boot: str) -> dict[str, dict[str, object]]:
    if not os.path.lexists(path):
        return {}
    fd = _private_file(path)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if os.fstat(stream.fileno()).st_size > 4 * 1024 * 1024:
            raise StartupRecoveryError("startup recovery attempt record is oversized")
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise StartupRecoveryError("startup recovery attempt record is invalid")
    schema = payload.get("schema")
    attempts = payload.get("attempts")
    if not isinstance(attempts, dict):
        raise StartupRecoveryError("startup recovery attempt entries are invalid")
    if schema == "termdeck.startup-recovery-attempts.v1":
        if not all(isinstance(key, str) and isinstance(value, str)
                   and _DIGEST.fullmatch(value) for key, value in attempts.items()):
            raise StartupRecoveryError("startup recovery attempt entries are invalid")
        # A historical pre-submit digest proves no terminal outcome.
        attempts = {key: {"digest": value, "state": "legacy-unknown"}
                    for key, value in attempts.items()}
    elif schema == _ATTEMPT_SCHEMA:
        for key, entry in attempts.items():
            if (not isinstance(key, str) or not isinstance(entry, dict)
                    or not isinstance(entry.get("digest"), str)
                    or not _DIGEST.fullmatch(entry["digest"])
                    or entry.get("state") not in {"pending", "ready", "failed", "legacy-unknown"}):
                raise StartupRecoveryError("startup recovery attempt entries are invalid")
            if entry["state"] != "legacy-unknown" and (
                type(entry.get("owner_pid")) is not int or entry["owner_pid"] <= 1
                or not isinstance(entry.get("owner_start"), str)
                or not entry["owner_start"].isdigit()
                or entry.get("owner_boot") != payload.get("boot_id")
            ):
                raise StartupRecoveryError("startup recovery attempt owner is invalid")
            if (type(entry.get("interrupted_retries", 0)) is not int
                    or not 0 <= entry.get("interrupted_retries", 0) <= 1):
                raise StartupRecoveryError("startup recovery retry budget is invalid")
    else:
        raise StartupRecoveryError("startup recovery attempt record is invalid")
    return dict(attempts) if payload.get("boot_id") == boot else {}


def _save_attempts(path: Path, boot: str, attempts: dict[str, dict[str, object]]) -> None:
    # The pending phase is durable before submission. No private launch text is
    # stored, and only an explicit quiescent retry can re-admit unchanged input.
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema": _ATTEMPT_SCHEMA, "boot_id": boot,
                       "attempts": attempts}, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(path):
            check = _private_file(path)
            os.close(check)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _input_digest(record) -> str:
    fields = ("id", "tmux_name", "pane_target", "command", "cwd",
              "recovery_command", "recovery_cwd", "recovery_recipe",
              "recovery_recipe_sha256", "restart_policy", "auth_scope_id",
              "codex_home", "selected_account", "auth_changed_at", "recovery_intent", "external")
    encoded = json.dumps({name: getattr(record, name, None) for name in fields},
                         sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _attempt_owner_alive(entry, boot: str) -> bool:
    if entry.get("state") == "legacy-unknown":
        return False  # Native writer locks/claims and a fresh pane proof remain required.
    if entry.get("owner_boot") != boot:
        return False
    try:
        return _process_start(entry["owner_pid"]) == entry["owner_start"]
    except FileNotFoundError:
        return False
    except (OSError, IndexError, KeyError, TypeError):
        return True


def _retry_is_quiescent(record, entry, digest, boot, store, backend, coordinator) -> bool:
    if _attempt_owner_alive(entry, boot):
        return False
    try:
        with coordinator.metadata_writer_guard([record]):
            current = store.get(record.id)
            if current is None or _input_digest(current) != digest:
                return False
            coordinator.assert_metadata_quiescent([current])
            observation = backend.reconciliation_snapshot([current]).get(current.id, {})
            return observation.get("state") in {"missing", "dead"}
    except Exception:
        return False


def recover_saved_terminals(store, backend, *, runtime_root: Path,
                            max_workers: int = 4, readiness_timeout: float = 120,
                            session_ids: set[str] | None = None,
                            boot_id: str | None = None, retry_quiescent: bool = False,
                            deferred_native_accounts: set[str] | None = None,
                            overall_timeout: float = 1800) -> dict[str, object]:
    """Recover durably eligible missing panes through the shared owner.

    Live panes receive no input or lifecycle action. Previously ready attempts
    may recover after their owner disappears and the pane is authoritatively
    lost. Interrupted attempts have one automatic quiescent retry per input and
    boot. Failed/legacy attempts still require exact operator selection.
    """
    if (not math.isfinite(overall_timeout) or overall_timeout <= 0
            or not math.isfinite(readiness_timeout) or readiness_timeout <= 0):
        raise StartupRecoveryError("startup recovery timing is invalid")
    if retry_quiescent and not session_ids:
        raise StartupRecoveryError("quiescent retry requires exact session selection")
    boot = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    outcomes: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + overall_timeout
    with startup_lock(runtime_root) as acquired:
        if not acquired:
            return {"action": "startup-recover", "state": "already-owned",
                    "all_eligible_ready": False, "sessions": []}
        records = store.list(include_archived=True)
        if session_ids is not None:
            records = [record for record in records if record.id in session_ids]
            if session_ids - {record.id for record in records}:
                raise StartupRecoveryError("selected session is absent from the registry")
        attempts_path = runtime_root / "startup-recovery-attempts.json"
        attempts = _load_attempts(attempts_path, boot)
        snapshot = backend.reconciliation_snapshot(records)
        owner = {"owner_pid": os.getpid(), "owner_start": _process_start(os.getpid()),
                 "owner_boot": boot}
        pending: set[str] = set()
        condition = threading.Condition()
        callbacks_closed = False
        journal_failed = False

        def completed(result):
            nonlocal journal_failed
            with condition:
                if callbacks_closed or result.session_id not in pending:
                    return
                item = {"id": result.session_id, "state": result.state,
                        "created": result.created, "pid": result.pid}
                if result.error:
                    item["reason"] = "recovery-failed"
                    item["detail_sha256"] = hashlib.sha256(result.error.encode()).hexdigest()
                outcomes[result.session_id] = item
                attempts[result.session_id]["state"] = "ready" if result.state == "ready" else "failed"
                try:
                    _save_attempts(attempts_path, boot, attempts)
                except Exception:
                    journal_failed = True
                pending.discard(result.session_id)
                condition.notify_all()

        coordinator = RecoveryCoordinator(backend, store, max_workers=max_workers,
            readiness_timeout=readiness_timeout, allow_auth_scope_rebind=False,
            readiness_failure_grace=min(1.0, readiness_timeout))
        worker_quiescent = False
        try:
            for record in records:
                if time.monotonic() >= deadline:
                    outcomes[record.id] = {"id": record.id, "state": "blocked", "reason": "worker-deadline"}
                    continue
                observation = snapshot.get(record.id, {"state": "unavailable"})
                state = observation.get("state")
                if state == "live":
                    pid = observation.get("pid")
                    if (backend.reconciliation_identity_current(observation)
                            and pid is not None and coordinator._known_live_ready(record, int(pid))):
                        outcomes[record.id] = {"id": record.id, "state": "already-live",
                                              "pid": pid, "created": False}
                    else:
                        outcomes[record.id] = {"id": record.id, "state": "blocked",
                                              "reason": ("pane-identity-changed" if not backend.reconciliation_identity_current(observation)
                                                         else "live-pane-not-ready")}
                    continue
                try:
                    allowed = startup_recovery_allowed(record)
                    if record.status in {"running", "paused", "missing", "recovery-pending", "start-error"}:
                        recipe_from_record(record)
                except SessionRecipeError:
                    outcomes[record.id] = {"id": record.id, "state": "blocked",
                                          "reason": "saved-recipe-invalid"}
                    continue
                if not allowed:
                    outcomes[record.id] = {"id": record.id, "state": "skipped",
                                          "reason": "recovery-inactive"}
                    continue
                reason = None
                if state not in {"missing", "dead"}:
                    reason = "pane-owner-unobservable"
                else:
                    recipe = recipe_from_record(record)
                    lifecycle_probe = getattr(backend, "_pane_lifecycle", None)
                    if (state == "dead" and recipe.restart_policy == "on-failure"
                            and callable(lifecycle_probe)):
                        lifecycle = lifecycle_probe(record)
                        if (lifecycle is not None and lifecycle.dead
                                and lifecycle.exit_status in {0, 130}):
                            # A completed application is not a failed launch.
                            # Reconcile only its runtime status, retaining the
                            # original command, recipe, scope and pane history.
                            with coordinator.metadata_writer_guard([record]):
                                coordinator.assert_metadata_quiescent([record])
                                unchanged = (store.get(record.id) == record
                                             and lifecycle_probe(record) == lifecycle)
                                accepted = store.reconcile_runtime_states(
                                    [(record.id, "stopped", record.visible, None)],
                                    {record.id: record}) if unchanged else set()
                            outcomes[record.id] = {"id": record.id,
                                "state": "skipped" if record.id in accepted else "blocked",
                                "reason": "application-completed" if record.id in accepted
                                          else "pane-identity-changed"}
                            continue
                    if not Path(recipe.cwd).is_dir():
                        reason = "saved-working-directory-unavailable"
                    elif ((record.selected_account or "") in (deferred_native_accounts or set())
                          and coordinator._is_codex_recovery(record)):
                        reason = "native-account-work-deferred"
                    elif (record.codex_home or record.auth_scope_id) and not (
                        coordinator._record_uses_saved_auth_scope(record, store)
                    ):
                        reason = "saved-private-auth-scope-unavailable"
                if reason:
                    outcomes[record.id] = {"id": record.id, "state": "blocked", "reason": reason}
                    continue
                digest = _input_digest(record)
                entry = attempts.get(record.id)
                interrupted_retries = 0
                if entry is not None and entry["digest"] == digest:
                    interrupted_retries = entry.get("interrupted_retries", 0)
                    interrupted = entry["state"] == "pending" and interrupted_retries < 1
                    if not (retry_quiescent or entry["state"] == "ready" or interrupted) or not _retry_is_quiescent(
                        record, entry, digest, boot, store, backend, coordinator
                    ):
                        outcomes[record.id] = {"id": record.id, "state": "blocked",
                                              "reason": "already-attempted-this-boot",
                                              "attempt_state": entry["state"]}
                        continue
                    if interrupted and not retry_quiescent:
                        interrupted_retries += 1
                    elif entry["state"] == "ready":
                        interrupted_retries = 0
                with condition:
                    attempts[record.id] = {"digest": digest, "state": "pending",
                                          "interrupted_retries": interrupted_retries, **owner}
                    _save_attempts(attempts_path, boot, attempts)
                    pending.add(record.id)
                try:
                    submission = coordinator.submit(record, on_ready=completed, on_failed=completed)
                    with condition:
                        if not submission.accepted and record.id in pending:
                            outcomes[record.id] = {"id": record.id, "state": "failed",
                                                  "reason": "submission-rejected"}
                            attempts[record.id]["state"] = "failed"
                            _save_attempts(attempts_path, boot, attempts)
                            pending.discard(record.id)
                except Exception as exc:
                    with condition:
                        outcomes[record.id] = {"id": record.id, "state": "failed",
                            "reason": "submission-failed", "error_type": type(exc).__name__}
                        attempts[record.id]["state"] = "failed"
                        _save_attempts(attempts_path, boot, attempts)
                        pending.discard(record.id)
            queue_deadline = min(deadline, time.monotonic()
                + readiness_timeout * (1 + len(records) / max(1, max_workers)) + 10)
            with condition:
                while pending and time.monotonic() < queue_deadline:
                    condition.wait(min(1, max(0, queue_deadline - time.monotonic())))
                # Unknown claimed work remains pending durably. Never call it ready
                # and never remove its owner claims merely because a deadline passed.
                for session_id in pending:
                    outcomes[session_id] = {"id": session_id, "state": "pending",
                                            "reason": "readiness-deadline"}
                callbacks_closed = True
        finally:
            with condition:
                callbacks_closed = True
            worker_quiescent = coordinator.shutdown(wait=True, timeout=0.5, persist_cancelled=False)
        # A launcher may exit after its initial callback while other tabs are
        # still recovering. Report fresh pane evidence, never a running flag or
        # the callback alone. Leave metadata reconciliation to its guarded owner.
        if worker_quiescent:
            invalidate = getattr(backend, "invalidate_cache", None)
            if callable(invalidate):
                invalidate()
            for record in records:
                item = outcomes.get(record.id, {})
                if item.get("state") != "ready":
                    continue
                pid = item.get("pid")
                try:
                    ready = (pid is not None
                             and coordinator._known_live_ready(record, int(pid)))
                except (OSError, ValueError, RuntimeError):
                    ready = False
                if not ready:
                    item.update(state="failed", reason="payload-exited-after-readiness")
                    attempts[record.id]["state"] = "failed"
                    try:
                        _save_attempts(attempts_path, boot, attempts)
                    except Exception:
                        journal_failed = True
        counts = dict(Counter(item["state"] for item in outcomes.values()))
        complete = worker_quiescent and not journal_failed and not pending and len(outcomes) == len(records)
        return {"action": "startup-recover", "boot_id": boot,
                "state": "complete" if complete else "incomplete",
                "worker_quiescent": worker_quiescent, "journal_durable": not journal_failed,
                "all_eligible_ready": complete and not any(counts.get(s) for s in ("blocked", "failed", "pending")),
                "counts": counts, "sessions": list(outcomes.values())}


def run_managed_recovery(database: Path, *, managed_root: Path,
                         max_workers: int = 4, readiness_timeout: float = 120,
                         session_ids: set[str] | None = None,
                         retry_quiescent: bool = False,
                         deferred_native_accounts: set[str] | None = None,
                         overall_timeout: float = 1800) -> dict[str, object]:
    database = Path(os.path.abspath(database.expanduser()))
    state = database.parent
    if not database.is_file():
        raise StartupRecoveryError("saved session database is unavailable")
    require_storage_fabric(managed_root, db_path=database,
        auth_root=state / "terminal-auth", tmux_runtime=state / "tmux",
        runtime_root=state / "runtime", checkpoint_root=state / "checkpoints", production=True)
    store = SessionStore(database, auth_root=state / "terminal-auth", provision_auth_scopes=False)
    try:
        return recover_saved_terminals(store, TmuxBackend(executable="/usr/bin/tmux"),
            runtime_root=state / "runtime", max_workers=max_workers,
            readiness_timeout=readiness_timeout, session_ids=session_ids,
            retry_quiescent=retry_quiescent, deferred_native_accounts=deferred_native_accounts,
            overall_timeout=overall_timeout)
    finally:
        store.close()


def _worker_deadline():
    # Only this headless CLI process exits. Terminal owners live in independent
    # lifecycle scopes, and durable claims require authoritative quiescence later.
    print(json.dumps({"action": "startup-recover", "state": "incomplete",
                      "reason": "worker-deadline"}), flush=True)
    os._exit(1)


def main(argv=None) -> int:
    import sys
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--record-output"]:
        from .output_journal import pipe_main
        return pipe_main(arguments[1:])
    if arguments[:1] == ["--mirror-recordings"]:
        from .output_journal import mirror_main
        return mirror_main(arguments[1:])
    if arguments[:1] == ["--track-directories"]:
        from .directory_tracking import main as track_directories
        return track_directories(arguments[1:])
    if arguments[:1] == ["--watch"]:
        from .session_durability import watch_recovery
        return watch_recovery(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--managed-root", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, choices=range(1, 17), default=4)
    parser.add_argument("--readiness-timeout", type=float, default=120)
    parser.add_argument("--overall-timeout", type=float, default=1800)
    parser.add_argument("--session-id", action="append", default=None)
    parser.add_argument("--retry-quiescent", action="store_true")
    parser.add_argument("--defer-native-account", action="append", default=[])
    args = parser.parse_args(argv)
    watchdog = None
    try:
        if (not 0 < args.readiness_timeout <= 3600
                or not 0 < args.overall_timeout <= 1800):
            raise StartupRecoveryError("readiness timeout is invalid")
        if args.retry_quiescent and not args.session_id:
            raise StartupRecoveryError("quiescent retry requires exact session selection")
        watchdog = threading.Timer(args.overall_timeout + 2, _worker_deadline)
        watchdog.daemon = True
        watchdog.start()
        result = run_managed_recovery(args.db or default_db_path(), managed_root=args.managed_root,
            max_workers=args.max_workers, readiness_timeout=args.readiness_timeout,
            session_ids=set(args.session_id) if args.session_id else None,
            retry_quiescent=args.retry_quiescent,
            deferred_native_accounts=set(args.defer_native_account),
            overall_timeout=args.overall_timeout)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not result.get("worker_quiescent", True):
            # ThreadPoolExecutor atexit joins can otherwise defeat the deadline.
            os._exit(1)
        # Type=oneshot/RemainAfterExit treats zero as successful recovery. A
        # blocked tab, busy owner or nondurable journal is still incomplete,
        # even when no individual launch has reported failure.
        return 0 if result.get("all_eligible_ready") is True else 1
    except Exception as exc:
        print(json.dumps({"action": "startup-recover", "state": "failed",
                          "error_type": type(exc).__name__}, sort_keys=True), flush=True)
        if watchdog is not None:
            # An admission/cleanup exception may leave executor threads alive.
            # Exit only this owning CLI after durable failure output.
            os._exit(1)
        return 1
    finally:
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
