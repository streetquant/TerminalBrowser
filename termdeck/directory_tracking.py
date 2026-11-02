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
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with closing(SessionStore(args.db, provision_auth_scopes=False)) as store:
        backend = TmuxBackend(pane_cache_ttl=0)
        while True:
            result = save_directories(store, backend)
            if args.once or result["updated"] or result["failed"]:
                print(json.dumps({"action": "save-directories", **result}), flush=True)
            if args.once or stopped.wait(args.interval):
                # Final observation while tmux still exists on normal shutdown.
                if not args.once:
                    save_directories(store, backend)
                return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
