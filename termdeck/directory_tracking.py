"""Persist live terminal directories without a desktop window or browser client."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

from .session_store import SessionStore
from .tmux_backend import TmuxBackend

SERVICE = "termdeck-directory-tracker.service"


def save_directories(store: SessionStore, backend: TmuxBackend) -> dict:
    """Use one live pane snapshot; never infer a missing pane's cwd as HOME.

    Read each record again under the write lock so unrelated lifecycle/name
    changes cannot be overwritten by an earlier inventory. Store.update owns
    the canonical recipe, digest and recovery-authority invalidation.
    """
    records = [r for r in store.list() if not r.external and r.status == "running"]
    directories = backend.current_directories(records)
    changed = failed = 0
    for observed in records:
        directory = directories.get(observed.id)
        if not directory or directory == observed.cwd or not Path(observed.cwd).is_dir():
            # A reboot may temporarily fall back while a mount/directory is
            # absent. Keep the intended path rather than replacing it with
            # that fallback merely because it is currently visible.
            continue
        try:
            store.conn.execute("BEGIN IMMEDIATE")
            current = store.get(observed.id)
            if (current is None or current.external or current.status != "running"
                    or current.tmux_name != observed.tmux_name or current.cwd != observed.cwd):
                store.conn.rollback()
                continue
            changes = {"cwd": directory, "last_opened_at": current.last_opened_at}
            # An exact foreground recovery command has its own observed cwd.
            # A plain shell must not keep an obsolete recovery cwd instead of
            # the directory the user just navigated to.
            if not current.recovery_command:
                changes["recovery_cwd"] = None
            store.update(current.id, **changes)
            changed += 1
        except (OSError, ValueError, sqlite3.Error):
            store.conn.rollback()
            failed += 1
    return {"selected": len(records), "updated": changed, "failed": failed}


def save_recovery_metadata(store, backend, root):
    """Observe app-native IDs without a UI; retain history before recipe changes."""
    from .session_durability import _atomic, _directory, _sync
    from uuid import uuid4
    from .recovery_authority_context import record_cas
    records = [r for r in store.list() if not r.external and r.status == "running"]
    batch = backend.recovery_observations(records, native_capture=True)
    updates = []
    by_id = {record.id: record for record in records}
    try:
        store.conn.execute("BEGIN IMMEDIATE")
        for session_id, (command, cwd) in batch.metadata.items():
            previous = by_id.get(session_id)
            current = store.get(session_id)
            if previous is None or current is None or record_cas(current) != record_cas(previous):
                continue
            observation = batch.authority.get(session_id)
            if observation is not None and observation.is_current is not None and not observation.is_current():
                continue
            if command and (command != previous.recovery_command or cwd != previous.recovery_cwd):
                directory = _directory(_directory(_directory(root) / "applications") / session_id)
                _sync(directory.parent)
                _atomic(directory / (str(time.time_ns()) + "-" + uuid4().hex + ".json"),
                        json.dumps(dict(session_id=session_id, command=command, cwd=cwd)).encode())
            updates.append((session_id, command, cwd))
        count = store.update_recovery_metadata(updates, authority_observations=batch.authority)
        store.conn.commit()
        return count
    except BaseException:
        store.conn.rollback()
        raise


def stop_recorders(recorders, timeout=7.0):
    """One fleet-wide deadline, including stuck transports or disk I/O.

    Each append has already flushed before returning. Uncommitted final work
    is never advertised as durable; timeout does not touch any pane process.
    """
    import queue
    pending = queue.Queue()
    for item in recorders.values():
        pending.put(item)
    deadline = time.monotonic() + timeout
    def close_worker():
        while time.monotonic() < deadline:
            try:
                controller, journal, _pid = pending.get_nowait()
            except queue.Empty:
                return
            try:
                controller.stop()
                if journal is not None:
                    journal.close()
            finally:
                pending.task_done()
    workers = [threading.Thread(target=close_worker, daemon=True) for _ in range(min(32, len(recorders)))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    return pending.unfinished_tasks


class PipeObserver:
    """Local tracking only: stopping the observer never removes a tmux pipe."""
    def __init__(self, root, session_id, pane_id, server_pid, server_start):
        self.root, self.session_id, self.pane_id = root, session_id, pane_id
        self.server_pid, self.server_start = server_pid, server_start
        self._stopped = False

    @property
    def _ready(self):
        from .output_journal import pipe_owner
        return bool(pipe_owner(self.root, self.session_id, self.pane_id,
                               self.server_pid, self.server_start))

    def stop(self):
        self._stopped = True


def maintain_recorders(store, backend, root, recorders, *, recorder_launcher=None):
    """Install output-only pipes without clients, resize, input or app lifecycle.

    `-o` refuses to replace an occupied pipe, including a race with another
    installer. Existing user pipes are reported and never toggled or removed.
    Verified sinks remain owned by tmux across observer restarts.
    """
    import shlex
    from uuid import uuid4
    from .resources import _parse_stat
    from .output_journal import pipe_owner, journal_directory
    from .session_durability import _atomic, MAX_SCREEN_BYTES
    from .startup_publication import verify_bundle
    records = [r for r in store.list() if not r.external and r.status in ("running", "recovery-pending")]
    result = backend._run(["list-panes", "-a", "-F",
        "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_width}\t#{pane_height}\t#{pane_pipe}\t#{pid}"],
        check=False, timeout=2)
    if result.returncode:
        return len(recorders)  # A failed inventory cannot retire a live recorder.
    panes = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 7:
            panes[fields[0]] = fields[1:]
    launcher = Path(recorder_launcher) if recorder_launcher else Path(__file__).resolve().parents[2] / "launch.py"
    # This entry point can only start an immutable, verified native payload.
    # Cache one verification for this observer; old sinks keep their own bundle.
    if getattr(backend, "_recorder_launcher", None) != launcher:
        verify_bundle(launcher.parent)
        backend._recorder_launcher = launcher
    attempts = getattr(backend, "_recorder_attempts", {})
    backend._recorder_attempts = attempts
    geometry_cache = getattr(backend, "_recorder_geometry", {})
    backend._recorder_geometry = geometry_cache
    eligible = {record.id for record in records if record.tmux_name in panes}
    for identity in set(recorders) - eligible:
        recorders.pop(identity)
    deadline = time.monotonic() + 3
    started = 0
    for record in records:
        fields = panes.get(record.tmux_name)
        if fields is None:
            continue
        pane_id, pid, cols, rows, piped, server_pid = fields
        pid, server_pid = int(pid), int(server_pid)
        pane, server = _parse_stat(pid), _parse_stat(server_pid)
        if pane is None or server is None:
            continue
        server_start = server["starttime"]
        observer = PipeObserver(root, record.id, pane_id, server_pid, server_start)
        geometry_key = (pane_id, pid, pane["starttime"], server_pid, server_start)
        def publish_pane():
            if geometry_cache.get(record.id) != geometry_key:
                _atomic(journal_directory(root, record.id) / "pane-current.json", json.dumps(dict(
                    pane_id=pane_id, pid=pid, starttime=pane["starttime"],
                    server_pid=server_pid, server_start=server_start)).encode())
                geometry_cache[record.id] = geometry_key
        if piped == "1":
            owner = pipe_owner(root, record.id, pane_id, server_pid, server_start)
            if owner is not None:
                publish_pane()
                recorders[record.id] = (observer, None, pid)
            elif record.id in recorders and time.monotonic() - attempts.get(record.id, -60) < 10:
                pass  # Immutable payload verification is still starting.
            elif time.monotonic() - attempts.get(record.id, -60) >= 60:
                attempts[record.id] = time.monotonic()
                print(json.dumps({"action": "record-output", "session_id": record.id,
                    "coverage": "existing-unowned-pipe", "preserved": True}), flush=True)
                recorders.pop(record.id, None)
            continue
        if record.id in recorders:
            recorders.pop(record.id)
            print(json.dumps({"action": "record-output", "session_id": record.id,
                "coverage": "sink-unavailable", "retry_after_seconds": 60}), flush=True)
        if started >= 16 or time.monotonic() >= deadline or time.monotonic() - attempts.get(record.id, -60) < 60:
            continue
        attempts[record.id] = time.monotonic()
        token = uuid4().hex
        publish_pane()
        command = shlex.join(["exec", "/usr/bin/python3", "-I", "-B", str(launcher),
            "--record-output", "--root", str(root), "--session-id", record.id,
            "--pane-id", pane_id, "--pane-pid", str(pid), "--pane-start", str(pane["starttime"]),
            "--server-pid", str(server_pid), "--server-start", str(server_start),
            "--columns", cols, "--rows", rows, "--token", token])
        # One server-side command list prevents a capture-before-attach gap.
        install = backend._run(["pipe-pane", "-o", "-O", "-t", pane_id, command,
            ";", "capture-pane", "-p", "-e", "-J", "-t", pane_id, "-S", "-10000"],
            check=False, timeout=2)
        replay = b""
        if not install.returncode and len(install.stdout.encode()) <= MAX_SCREEN_BYTES:
            replay = b"\x1b[0m\x1b[2J\x1b[H" + install.stdout.replace("\n", "\r\n").encode()
        _atomic(journal_directory(root, record.id) / (token + ".initial"), replay)
        if install.returncode:
            print(json.dumps({"action": "record-output", "session_id": record.id,
                "coverage": "pipe-install-failed"}), flush=True)
            continue
        recorders[record.id] = (observer, None, pid)
        started += 1
    return len(recorders)


def install(source: Path, managed_root: Path, state_root: Path, unit_dir: Path,
            backup_path: Path) -> dict:
    """Publish a separate verified observer; leave terminal/web services live."""
    from . import startup_publication as publication
    from .release_manager import _publish_files_transactionally

    prepared = publication.prepare(source, managed_root)
    bundle = Path(prepared["bundle"])
    publication.verify_bundle(bundle)
    units = publication._directory(unit_dir)
    state = publication._directory(state_root)
    base = publication._directory(managed_root / "startup-recovery")
    target = units / SERVICE
    # Reuse the existing environment/path quoting and shutdown ordering.
    unit = publication._unit(bundle, managed_root, state)
    unit = unit.replace(b"Description=Restore saved TermDeck terminals",
                        b"Description=Save live TermDeck working directories")
    unit = unit.replace(b"Type=oneshot\nRemainAfterExit=yes\nTimeoutStartSec=infinity",
                        b"Type=simple\nRestart=on-failure\nRestartSec=2\nTimeoutStopSec=10")
    unit = b"\n".join(b"TimeoutStopSec=30" if line.startswith(b"TimeoutStopSec=") else line
                      for line in unit.splitlines()) + b"\n"
    command = next(line for line in unit.splitlines() if line.startswith(b"ExecStart="))
    launch_argument = os.fsencode(bundle / "launch.py") + b'"'
    command = command[:command.index(launch_argument) + len(launch_argument)]
    command += (b' "--track-directories" "--db" "' + os.fsencode(state / "sessions.sqlite3")
                + b'" "--managed-root" "' + os.fsencode(managed_root) + b'"')
    unit = b"\n".join(command if line.startswith(b"ExecStart=") else line
                      for line in unit.splitlines()) + b"\n"
    with publication._lock(base):
        original = publication._read(target, absent=True)
        publication._directory(backup_path.parent)
        publication._write_new(backup_path, publication._json({
            "unit_path": str(target), "preimage": publication._encode_snapshot(original),
            "postimage_sha256": publication._digest(unit), "bundle": str(bundle)}), 0o600)
        publication._sync(backup_path.parent)

        def verify():
            publication.verify_bundle(bundle)
            if publication._read(target)["content"] != unit:
                raise publication.StartupPublicationError("directory tracker readback mismatch")

        _publish_files_transactionally(((target, unit, 0o644, "directory tracker"),),
            expected_preimages={target: original}, verify=verify,
            journal_path=units / ".termdeck-publication-directories.json")
    return {"unit_path": str(target), "bundle": str(bundle),
            "bundle_id": prepared["bundle_id"], "backup_path": str(backup_path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--managed-root", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args(argv)
    if not 0.25 <= args.interval <= 60 or not args.db.is_file():
        parser.error("existing database and interval between 0.25 and 60 required")
    if os.environ.get("TERMDECK_STATE_ROOT"):
        if args.managed_root is None:
            parser.error("managed runtime requires --managed-root")
        from .storage_fabric import require_storage_fabric
        state = args.db.parent
        require_storage_fabric(args.managed_root, db_path=args.db,
            auth_root=state / "terminal-auth", tmux_runtime=state / "tmux",
            runtime_root=state / "runtime", checkpoint_root=state / "checkpoints", production=True)
    stopped = threading.Event()
    shutdown_deadline = [None]
    def stop_signal(*_):
        if shutdown_deadline[0] is None:
            shutdown_deadline[0] = time.monotonic() + 25
        stopped.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_signal)
    with closing(SessionStore(args.db, provision_auth_scopes=False)) as store:
        backend = TmuxBackend(pane_cache_ttl=0)
        from .session_durability import save_screens, backup_registry
        next_screen = next_backup = 0.0
        recorders = {}
        while True:
            result = save_directories(store, backend)
            now = time.monotonic()
            try:
                maintain_recorders(store, backend, args.db.parent / "durability", recorders)
                save_recovery_metadata(store, backend, args.db.parent / "durability")
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                result["failed"] += 1
                print(json.dumps({"action": "record-output", "failed": True, "error_type": type(exc).__name__}), flush=True)
            for action, due, period in (("screens", next_screen, 5), ("registry", next_backup, 60)):
                if now < due:
                    continue
                try:
                    if action == "screens":
                        detail = save_screens(store, backend, args.db.parent / "durability")
                        next_screen = now + period
                    else:
                        detail = {"path": str(backup_registry(store, args.db.parent / "durability"))}
                        next_backup = now + period
                    print(json.dumps({"action": "save-" + action, **detail}), flush=True)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    # Storage failure is visible and cannot stop directory
                    # preservation or erase the previous good snapshots.
                    result["failed"] += 1
                    print(json.dumps({"action": "save-" + action, "failed": True,
                                      "error_type": type(exc).__name__}), flush=True)
                    if action == "screens":
                        next_screen = now + period
                    else:
                        next_backup = now + period
            if args.once or result["updated"] or result["failed"]:
                print(json.dumps({"action": "save-directories", **result}), flush=True)
            if args.once or stopped.wait(args.interval):
                # One deadline covers all final saves AND fleet drain. A
                # wedged storage device cannot turn a graceful stop into an
                # unbounded serial join; systemd retains five seconds margin.
                deadline = shutdown_deadline[0] or time.monotonic() + 25
                final_errors = []
                final_worker = None
                if not args.once:
                    def final_save():
                        try:
                            with closing(SessionStore(args.db, provision_auth_scopes=False)) as final_store:
                                final_backend = TmuxBackend(pane_cache_ttl=0)
                                save_directories(final_store, final_backend)
                                if time.monotonic() < deadline:
                                    save_screens(final_store, final_backend, args.db.parent / "durability")
                                if time.monotonic() < deadline:
                                    backup_registry(final_store, args.db.parent / "durability")
                        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                            final_errors.append(type(exc).__name__)
                    final_worker = threading.Thread(target=final_save, daemon=True)
                    final_worker.start()
                remaining = stop_recorders(recorders, timeout=max(0, min(7, deadline - time.monotonic())))
                if final_worker is not None:
                    final_worker.join(max(0, deadline - time.monotonic()))
                    if final_worker.is_alive():
                        final_errors.append("shutdown-deadline")
                if final_errors:
                    result["failed"] += len(final_errors)
                    print(json.dumps({"action": "final-durability-save", "errors": final_errors}), flush=True)
                if remaining:
                    print(json.dumps({"action": "stop-recorders", "unfinished": remaining}), flush=True)
                return 1 if result["failed"] or remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
