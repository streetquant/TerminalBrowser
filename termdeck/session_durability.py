"""Private, crash-safe copies of terminal screens and the saved-tab registry.

Screens are observations, not process checkpoints or an unbounded output log.
Keep each process generation separately so a fresh shell cannot erase the last
screen of the application it replaces. Never feed saved output into a live PTY.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4

MAX_SCREEN_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def _directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError("unsafe durability directory")
    return path


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Replace a directory entry, never follow an existing leaf symlink.
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(root: Path):
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("unsafe durability lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save_screens(store, backend, root: Path) -> dict:
    root = _directory(root)
    saved = failed = 0
    with _lock(root):
        records = [r for r in store.list(include_archived=True) if not r.external]
        start = getattr(backend, "_durability_screen_cursor", 0) % max(1, len(records))
        records = records[start:] + records[:start]
        observations = backend.reconciliation_snapshot(records)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        deadline = time.monotonic() + 5
        for index, record in enumerate(records):
            if time.monotonic() >= deadline:
                failed += 1  # Keep the observer/shutdown bounded; retain old copies.
                break
            backend._durability_screen_cursor = (start + index + 1) % max(1, len(records))
            before = observations.get(record.id, {})
            if before.get("state") != "live":
                continue  # Absence must never replace a previously saved screen.
            try:
                if not _ID.fullmatch(record.id) or not backend.reconciliation_identity_current(before):
                    raise OSError("screen owner changed")
                result = backend._run(["capture-pane", "-p", "-t", backend.pane_target(record),
                                       "-S", "-10000"], check=False)
                if result.returncode or len(result.stdout.encode()) > MAX_SCREEN_BYTES:
                    raise OSError("screen capture unavailable or oversized")
                after = backend.reconciliation_snapshot([record]).get(record.id, {})
                if before != after or not backend.reconciliation_identity_current(after):
                    raise OSError("screen owner changed during capture")
                generation = hashlib.sha256(json.dumps([boot, before], sort_keys=True).encode()).hexdigest()
                directory = _directory(root / "screens" / record.id)
                payload = {"schema": "termdeck.saved-screen.v1", "session_id": record.id,
                           "generation": generation, "boot_id": boot, "owner": before,
                           "captured_at": datetime.now(timezone.utc).isoformat(),
                           "history_line_limit": 10000, "text": result.stdout,
                           "text_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()}
                _atomic(directory / (generation + ".json"), (json.dumps(payload) + "\n").encode())
                saved += 1
            except (OSError, ValueError, RuntimeError):
                failed += 1
    return {"saved": saved, "failed": failed}


def backup_registry(store, root: Path) -> Path:
    """SQLite online backup includes committed WAL state; never copy a live DB."""
    root = _directory(root)
    with _lock(root):
        directory = _directory(root / "registry")
        # Retain every hourly generation, updating only the current hour.
        # No automatic deletion: retention is an explicit owner decision.
        name = datetime.now(timezone.utc).strftime("sessions-%Y%m%dT%H.sqlite3")
        destination = directory / name
        temporary = directory / (".backup-" + uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            deadline = time.monotonic() + 5
            def progress(*_):
                if time.monotonic() >= deadline:
                    raise TimeoutError("registry backup deadline exceeded")
            with closing(sqlite3.connect(temporary)) as target:
                store.conn.backup(target, pages=256, sleep=0.01, progress=progress)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise OSError("registry backup integrity failed")
                target.execute("PRAGMA journal_mode=DELETE")
            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, destination)
            _sync(directory)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-journal").unlink(missing_ok=True)


def watch_recovery(arguments, *, interval=30.0) -> int:
    """Run serial bounded workers. A failed tab is not automatically retried.

    Each worker exits after its attempt, so an old ready attempt never remains
    owned by the long-lived supervisor when its terminal disappears later.
    """
    if not 1 <= interval <= 300 or "--watch" in arguments:
        raise ValueError("invalid recovery watch arguments")
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    command = [sys.executable, "-I", "-B", str(Path(sys.argv[0]).resolve()), *arguments]
    while not stopped.is_set():
        worker = subprocess.Popen(command)
        try:
            while worker.poll() is None:
                if stopped.wait(0.25):
                    try:
                        worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.terminate()  # This exact child; terminals own separate scopes.
                        try:
                            worker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            worker.kill()
                    break
        finally:
            worker.wait()
        if stopped.wait(interval):
            break
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--generation", help="saved generation SHA256; default lists all snapshots")
    args = parser.parse_args(argv)
    if not _ID.fullmatch(args.session_id) or (args.generation and not re.fullmatch(r"[0-9a-f]{64}", args.generation)):
        parser.error("invalid session or generation")
    directory = args.root / "screens" / args.session_id
    if args.generation:
        path = directory / (args.generation + ".json")
        if path.resolve() != path.absolute():
            parser.error("saved screen path is not physical")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            payload = json.load(stream)
        if (payload["session_id"] != args.session_id or payload["generation"] != args.generation
                or hashlib.sha256(payload["text"].encode()).hexdigest() != payload["text_sha256"]):
            parser.error("saved screen integrity mismatch")
        print(payload["text"], end="")
    else:
        print(json.dumps({"session_id": args.session_id,
                          "snapshots": sorted(p.stem for p in directory.glob("*.json"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
