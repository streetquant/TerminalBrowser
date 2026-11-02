"""Explicit operator reenrollment of preserved, canonical recovery payloads.

This route never launches a terminal or relaxes normal startup validation.
An online registry backup and a reviewed plan containing full raw-row hashes
must exist before production use. Exact malformed preimages are durably
quarantined, shared recovery writer locks exclude GTK/headless writers, and
fresh authoritative missing-pane proof is checked inside the native CAS.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .recovery_coordinator import RecoveryCoordinator
from .session_recipe import AUTO_RESTART_POLICIES, parse_session_recipe, receipt_reenrollment
from .session_store import SessionStore
from .startup_recovery import StartupRecoveryError, _private_directory, startup_lock
from .storage_fabric import require_storage_fabric
from .tmux_backend import TmuxBackend


PLAN_SCHEMA = "termdeck.recovery-receipt-reenrollment-plan.v1"


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _entries(plan: dict) -> dict[str, dict]:
    if plan.get("schema") != PLAN_SCHEMA or not isinstance(plan.get("entries"), list):
        raise ValueError("receipt repair plan is invalid")
    result = {}
    for entry in plan["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id", "row_sha256", "payload_sha256", "new_digest"
        }:
            raise ValueError("receipt repair plan entry is invalid")
        session_id = entry["id"]
        if (not isinstance(session_id, str) or
                re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", session_id) is None or session_id in result):
            raise ValueError("receipt repair plan identity is invalid")
        if any(not isinstance(entry[field], str) or
               re.fullmatch(r"[0-9a-f]{64}", entry[field]) is None
               for field in ("row_sha256", "payload_sha256", "new_digest")):
            raise ValueError("receipt repair plan digest is invalid")
        result[session_id] = entry
    if not result or len(result) > 1000:
        raise ValueError("receipt repair plan selection is invalid")
    return result


def _write_quarantine(path: Path, payload: dict) -> dict[str, object]:
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError("receipt quarantine path must be physical and absolute")
    _private_directory(path.parent)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False) + "\n").encode("utf-8")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        current = path.parent.stat(follow_symlinks=False)
        if (parent.st_dev, parent.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("receipt quarantine directory identity changed")
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"path": str(path), "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def reenroll_saved_receipts(store, backend, *, plan: dict, runtime_root: Path,
                           quarantine_path: Path, writer_timeout: float = 1) -> dict[str, object]:
    entries = _entries(plan)
    boot = _boot_id()
    if plan.get("boot_id") != boot:
        raise ValueError("receipt repair plan boot identity changed")
    expected = {key: value["row_sha256"] for key, value in entries.items()}
    with startup_lock(runtime_root) as acquired:
        if not acquired:
            raise StartupRecoveryError("another startup recovery worker is active")
        rows = {}
        records = []
        for session_id, entry in sorted(entries.items()):
            row = store.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None or store.session_row_sha256(row) != entry["row_sha256"]:
                raise ValueError("receipt repair plan preimage changed")
            record = store._row_to_record(row)
            encoded, digest = receipt_reenrollment(record)
            if (digest != entry["new_digest"] or
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest() != entry["payload_sha256"]):
                raise ValueError("receipt repair plan payload changed")
            recipe = parse_session_recipe(encoded, digest)
            if (record.external or record.status not in {
                "running", "paused", "missing", "recovery-pending", "start-error"
            } or record.recovery_intent == "inactive" or recipe.restart_policy not in AUTO_RESTART_POLICIES):
                raise ValueError("receipt repair selection is not an active managed task")
            rows[session_id] = dict(row)
            records.append(record)
        coordinator = RecoveryCoordinator(backend, store, allow_auth_scope_rebind=False)
        observations = {}

        def assert_quiescent(current):
            if _boot_id() != boot:
                raise ValueError("receipt repair boot identity changed")
            coordinator.assert_metadata_quiescent(current)
            snapshot = backend.reconciliation_snapshot(current)
            if set(snapshot) != set(expected) or any(
                item.get("state") not in {"missing", "dead"} for item in snapshot.values()
            ):
                raise ValueError("receipt repair target is live or unobservable")
            observations.update({key: {"state": item["state"], "pid": item.get("pid")}
                                 for key, item in snapshot.items()})

        try:
            with coordinator.metadata_writer_guard(records, timeout=writer_timeout):
                quarantine = _write_quarantine(quarantine_path, {
                    "schema": "termdeck.recovery-receipt-preimages.v1", "boot_id": boot,
                    "old_receipts_accepted": False, "rows": rows,
                    "row_sha256": expected,
                })
                digests = store.reenroll_recovery_receipts(expected, assert_quiescent=assert_quiescent)
        finally:
            coordinator.shutdown(wait=False)
    return {"action": "reenroll-recovery-receipts", "state": "reenrolled", "boot_id": boot,
            "count": len(digests), "old_receipts_accepted": False, "payload_bytes_preserved": True,
            "terminal_actions": 0, "quarantine": quarantine,
            "observation_counts": dict(Counter(item["state"] for item in observations.values())),
            "sessions": [{"id": key, "new_digest": value} for key, value in sorted(digests.items())]}


def _read_plan(path: Path, expected_sha256: str) -> dict:
    if (not path.is_absolute() or path.resolve() != path or
            re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
        raise ValueError("receipt repair plan binding is invalid")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                stat.S_IMODE(before.st_mode) not in {0o400, 0o600} or before.st_nlink != 1 or
                before.st_size > 2 * 1024 * 1024):
            raise ValueError("receipt repair plan ownership is unsafe")
        encoded = stream.read(2 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size",
                  "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, key) != getattr(after, key) or
                getattr(after, key) != getattr(current, key) for key in fields) or
                hashlib.sha256(encoded).hexdigest() != expected_sha256):
            raise ValueError("receipt repair plan identity or content changed")
        plan = json.loads(encoded)
    _entries(plan)
    return plan


def run_managed_receipt_repair(database: Path, *, managed_root: Path,
                              plan_path: Path, plan_sha256: str,
                              quarantine_path: Path) -> dict[str, object]:
    database = Path(os.path.abspath(database.expanduser()))
    state = database.parent
    plan = _read_plan(plan_path, plan_sha256)
    require_storage_fabric(managed_root, db_path=database,
        auth_root=state / "terminal-auth", tmux_runtime=state / "tmux",
        runtime_root=state / "runtime", checkpoint_root=state / "checkpoints", production=True)
    store = SessionStore(database, auth_root=state / "terminal-auth", provision_auth_scopes=False)
    try:
        return reenroll_saved_receipts(store, TmuxBackend(executable="/usr/bin/tmux"), plan=plan,
            runtime_root=state / "runtime", quarantine_path=quarantine_path)
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--managed-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_managed_receipt_repair(args.db, managed_root=args.managed_root,
            plan_path=args.plan, plan_sha256=args.plan_sha256,
            quarantine_path=args.quarantine), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"action": "reenroll-recovery-receipts", "state": "failed",
                          "error_type": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
