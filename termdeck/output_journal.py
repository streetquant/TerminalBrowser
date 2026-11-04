"""Durable terminal output, independent of the lifetime of its renderer.

This records output, not application memory. UI writers commit before display;
the unattended tmux pipe is an asynchronous sink. Generations are never pruned.
The binary format is shared with the web server (T1000J1).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import threading
import time
from uuid import uuid4

from .session_durability import _atomic, _directory, _sync

MAGIC = b"T1000J1\n"
MAX_RECORD = 8 * 1024 * 1024 + 4
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_GENERATION = re.compile(r"[0-9]{20}-[0-9a-f]{32}\.tdj\Z")
_PIPE_BUNDLES = set()


def journal_directory(root: Path, session_id: str) -> Path:
    if not _ID.fullmatch(session_id):
        raise ValueError("invalid terminal identity")
    root = _directory(root)
    journals = _directory(root / "journals")
    _sync(root)
    directory = _directory(journals / session_id)
    _sync(journals)
    return directory


def _open_read(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink > 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("unsafe terminal recording")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class OutputJournal:
    def __init__(self, root: Path, session_id: str, *, primary: bool = True):
        self.directory = journal_directory(root, session_id)
        self.generation = f"{time.time_ns() // 1_000_000:020d}-{uuid4().hex}.tdj"
        self.path = self.directory / self.generation
        self._lock = threading.Lock()
        self._failed = False
        self._pointer = "latest" if primary else "latest-view"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._stream = os.fdopen(fd, "wb", buffering=0)
        self._backup_lock = None
        try:
            lock_fd = os.open(self.directory.parent.parent / ".journal-copy.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self._backup_lock = os.fdopen(lock_fd, "rb", buffering=0)
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise OSError("unsafe recording backup lock")
            self._write_all(MAGIC)
            os.fsync(fd)
            _sync(self.directory)
        except BaseException:
            self._stream.close()
            if self._backup_lock is not None:
                self._backup_lock.close()
            raise

    def _write_all(self, data: bytes):
        view = memoryview(data)
        while view:
            count = self._stream.write(view)
            if not count:
                raise OSError("terminal journal short write")
            view = view[count:]

    def append(self, data: bytes, *, geometry: tuple[int, int] | None = None, reset: bool = True):
        kind = (0 if reset else 2) if geometry is not None else 1
        if geometry is not None:
            cols, rows = geometry
            if not 2 <= cols <= 1000 or not 2 <= rows <= 500:
                raise ValueError("invalid terminal geometry")
            data = struct.pack(">HH", cols, rows) + data
        if len(data) > MAX_RECORD:
            raise ValueError("terminal recording frame too large")
        body = bytes([kind]) + data
        frame = struct.pack(">I", len(data)) + body + hashlib.sha256(body).digest()
        with self._lock:
            if self._failed:
                raise OSError("terminal recording previously failed; reconnect after fixing storage")
            try:
                # Separate tabs may flush concurrently. Explicit quiescent
                # copies may take EX; the normal mirror copies committed prefixes.
                # Screen/SQLite snapshots use a different lock.
                fcntl.flock(self._backup_lock.fileno(), fcntl.LOCK_SH)
                self._write_all(frame)
                os.fsync(self._stream.fileno())
                # Readers must never replay a complete but not-yet-fsynced
                # append. Publish its committed boundary before advertising it.
                _atomic(self.path.with_suffix(".tdj.commit"), str(self._stream.tell()).encode("ascii"))
                # Non-hidden pointer is included in the second-SSD mirror.
                _atomic(self.directory / self._pointer, self.generation.encode("ascii"))
            except BaseException:
                self._failed = True
                raise
            finally:
                fcntl.flock(self._backup_lock.fileno(), fcntl.LOCK_UN)

    def close(self):
        with self._lock:
            self._stream.close()
            self._backup_lock.close()


def read_page(root: Path, session_id: str, generation: str | None = None,
              offset: int = len(MAGIC), limit: int = 256 * 1024) -> dict:
    directory = journal_directory(root, session_id)
    if generation is None:
        candidates = []
        for name in ("latest-view", "latest"):
            try:
                with _open_read(directory / name) as pointer:
                    candidates.append((os.fstat(pointer.fileno()).st_mtime_ns,
                                       pointer.read(128).decode("ascii")))
            except FileNotFoundError:
                pass
        if not candidates:
            raise FileNotFoundError("no saved output")
        generation = max(candidates, key=lambda item: item[0])[1]
    if not _GENERATION.fullmatch(generation) or offset < len(MAGIC):
        raise ValueError("invalid terminal recording position")
    events, total = [], 0
    complete = truncated = False
    with _open_read((directory / generation).with_suffix(".tdj.commit")) as commit:
        value = commit.read(32)
    with _open_read(directory / generation) as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("invalid terminal recording header")
        size = os.fstat(stream.fileno()).st_size
        if not value.isdigit():
            raise ValueError("invalid recording commit boundary")
        committed = int(value)
        if not len(MAGIC) <= offset <= committed <= size:
            raise ValueError("terminal recording position past committed end")
        truncated = size > committed
        stream.seek(offset)
        while total < limit:
            start = stream.tell()
            if start == committed:
                complete = True
                break
            header = stream.read(min(5, committed - start))
            if not header:
                complete = True
                break
            if len(header) != 5:
                complete = truncated = True
                break
            length, kind = struct.unpack(">IB", header)
            if length > MAX_RECORD or kind not in (0, 1, 2) or (kind != 1 and length < 4):
                raise ValueError("corrupt terminal recording frame")
            if start + 5 + length + 32 > committed:
                raise ValueError("corrupt terminal recording commit boundary")
            data = stream.read(length)
            digest = stream.read(32)
            if len(data) != length or len(digest) != 32:
                complete = truncated = True
                break
            if hashlib.sha256(bytes([kind]) + data).digest() != digest:
                raise ValueError("terminal recording checksum mismatch")
            event = {"kind": kind}
            if kind != 1:
                cols, rows = struct.unpack(">HH", data[:4])
                if not 2 <= cols <= 1000 or not 2 <= rows <= 500:
                    raise ValueError("corrupt terminal recording geometry")
                event.update(cols=cols, rows=rows)
                data = data[4:]
            event["data_hex"] = data.hex()
            events.append(event)
            total += length
            offset = stream.tell()
    return dict(generation=generation, next_offset=offset, complete=complete,
                truncated_tail=truncated, events=events)


def pipe_owner(root: Path, session_id: str, pane_id: str, server_pid: int, server_start: int):
    """Prove a live sink before claiming an existing pipe as our recording."""
    from .resources import _parse_stat, _process_argv
    try:
        directory = journal_directory(root, session_id)
        with _open_read(directory / "pipe-owner.json") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            return None
        owner = json.loads(raw)
        if (owner["session_id"], owner["pane_id"], owner["server_pid"], owner["server_start"]) != (
                session_id, pane_id, server_pid, server_start):
            return None
        identity = _parse_stat(owner["pid"])
        if (identity is None or identity["starttime"] != owner["starttime"]
                or Path(f'/proc/{owner["pid"]}').stat().st_uid != os.geteuid()
                or identity["ppid"] != server_pid
                or _process_argv(owner["pid"]) != tuple(owner["argv"])):
            return None
        arguments = owner["argv"]
        if arguments[:3] != ["/usr/bin/python3", "-I", "-B"] or arguments[4] != "--record-output":
            return None
        flags = dict(zip(arguments[5::2], arguments[6::2]))
        if any(flags.get(key) != value for key, value in {
                "--root": str(root), "--session-id": session_id, "--pane-id": pane_id,
                "--server-pid": str(server_pid), "--server-start": str(server_start),
                "--token": owner["token"]}.items()):
            return None
        launcher = Path(arguments[3])
        if launcher not in _PIPE_BUNDLES:
            from .startup_publication import verify_bundle
            verify_bundle(launcher.parent)
            _PIPE_BUNDLES.add(launcher)
        return owner
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError):
        return None


def pipe_main(argv=None):
    """A tmux-owned output sink; it outlives every UI and observer process."""
    from .resources import _parse_stat
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--pane-pid", type=int, required=True)
    parser.add_argument("--pane-start", type=int, required=True)
    parser.add_argument("--server-pid", type=int, required=True)
    parser.add_argument("--server-start", type=int, required=True)
    parser.add_argument("--columns", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{32}", args.token) or not re.fullmatch(r"%[0-9]+", args.pane_id):
        parser.error("invalid recording identity")
    journal = None
    primary_lease = None
    try:
        pane, server = _parse_stat(args.pane_pid), _parse_stat(args.server_pid)
        if (pane is None or server is None or pane["starttime"] != args.pane_start
                or server["starttime"] != args.server_start or os.getppid() != args.server_pid):
            raise OSError("recording pane/server generation changed")
        directory = journal_directory(args.root, args.session_id)
        lease_fd = os.open(directory / ".primary.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        primary_lease = os.fdopen(lease_fd, "rb", buffering=0)
        info = os.fstat(lease_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("unsafe primary recording lease")
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = OutputJournal(args.root, args.session_id)
        # pipe-pane and capture-pane execute in one tmux command list, in that
        # order. Wait for the observer to publish that initial snapshot. If it
        # dies here, still retain the pipe's raw output rather than losing it.
        deadline = time.monotonic() + 3
        while True:
            try:
                with _open_read(journal.directory / (args.token + ".initial")) as initial:
                    replay = initial.read(MAX_RECORD)
                break
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    replay = b""
                    break
                time.sleep(.01)
        if len(replay) > MAX_RECORD - 4:
            raise OSError("recording initial replay too large")
        journal.append(replay, geometry=(args.columns, args.rows))
        own = _parse_stat(os.getpid())
        # /proc preserves the exact interpreter/immutable-launcher command,
        # unlike sys.argv, which Python has already normalized.
        from .resources import _process_argv
        owner = dict(session_id=args.session_id, pane_id=args.pane_id,
            pane_pid=args.pane_pid, pane_start=args.pane_start,
            server_pid=args.server_pid, server_start=args.server_start,
            pid=os.getpid(), starttime=own["starttime"], argv=_process_argv(os.getpid()),
            token=args.token, generation=journal.generation)
        _atomic(journal.directory / "pipe-owner.json", json.dumps(owner).encode())
        import select
        import termios
        geometry = (args.columns, args.rows)
        current_pid, current_start = args.pane_pid, args.pane_start
        while True:
            ready, _, _ = select.select([0], [], [], 1)
            try:
                with _open_read(journal.directory / "pane-current.json") as stream:
                    current_pane = json.loads(stream.read(4096))
                if (current_pane["pane_id"], current_pane["server_pid"], current_pane["server_start"]) == (
                        args.pane_id, args.server_pid, args.server_start):
                    current_pid, current_start = current_pane["pid"], current_pane["starttime"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            current = _parse_stat(current_pid)
            if current is not None and current["starttime"] == current_start:
                # Inspect size only; never retain a slave fd, read input,
                # resize, or become the terminal's controlling process.
                fd = None
                try:
                    fd = os.open(f"/proc/{current_pid}/fd/0", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
                    if os.isatty(fd):
                        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
                        if (cols, rows) != geometry and 2 <= cols <= 1000 and 2 <= rows <= 500:
                            geometry = (cols, rows)
                            journal.append(b"", geometry=geometry, reset=False)
                except OSError:
                    pass  # Output remains durable even if size is unavailable.
                finally:
                    if fd is not None:
                        os.close(fd)
            if not ready:
                continue
            data = os.read(0, 64 * 1024)
            if not data:
                break
            journal.append(data)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A dead sink is detected by the observer without touching the pane.
        print(f"terminal recording failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        try:
            directory = journal_directory(args.root, args.session_id)
            _atomic(directory / (args.token + ".failed.json"),
                    json.dumps({"error": str(exc), "type": type(exc).__name__}).encode())
        except (OSError, ValueError):
            pass
        return 1
    finally:
        if journal is not None:
            journal.close()
        if primary_lease is not None:
            primary_lease.close()


def mirror_journals(root: Path, destination: Path):
    """Copy committed immutable prefixes without pausing terminal writers."""
    source = _directory(root) / "journals"
    destination = _directory(destination)
    if root.resolve() == destination or root.resolve() in destination.parents or destination in root.resolve().parents:
        raise ValueError("recording mirror must be a separate tree")
    copied = skipped = total = 0
    if not source.exists():
        return dict(copied=0, skipped=0, bytes=0)
    for tab in source.iterdir():
        if not _ID.fullmatch(tab.name):
            continue
        directory = journal_directory(root, tab.name)
        target = journal_directory(destination, tab.name)
        for path in directory.iterdir():
            if not _GENERATION.fullmatch(path.name):
                continue
            try:
                commit = _open_read(path.with_suffix(".tdj.commit"))
            except FileNotFoundError:
                continue  # A constructor has not committed any output yet.
            with commit:
                value = commit.read(32)
            if not value.isdigit() or int(value) < len(MAGIC):
                raise ValueError("invalid source recording watermark")
            boundary = int(value)
            final = target / path.name
            try:
                with _open_read(final.with_suffix(".tdj.commit")) as existing:
                    previous = existing.read(32)
                with _open_read(final) as existing:
                    if previous.isdigit() and int(previous) >= boundary and os.fstat(existing.fileno()).st_size >= int(previous):
                        skipped += 1
                        continue
            except FileNotFoundError:
                pass
            temporary = target / ("." + path.name + "-" + uuid4().hex)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output, _open_read(path) as stream:
                    if stream.read(8) != MAGIC or os.fstat(stream.fileno()).st_size < boundary:
                        raise ValueError("invalid source recording prefix")
                    output.write(MAGIC)
                    while stream.tell() < boundary:
                        header = stream.read(5)
                        if len(header) != 5:
                            raise ValueError("short source recording frame")
                        length, kind = struct.unpack(">IB", header)
                        if length > MAX_RECORD or kind not in (0, 1, 2) or stream.tell() + length + 32 > boundary:
                            raise ValueError("invalid committed source frame")
                        payload, digest = stream.read(length), stream.read(32)
                        if len(payload) != length or hashlib.sha256(bytes([kind]) + payload).digest() != digest:
                            raise ValueError("source recording checksum mismatch")
                        output.write(header + payload + digest)
                    output.flush()
                    os.fsync(output.fileno())
                # The old committed prefix stays readable until this new file
                # is durable. Its larger watermark is published afterward.
                os.replace(temporary, final)
                _sync(target)
                _atomic(final.with_suffix(".tdj.commit"), str(boundary).encode())
                copied += 1
                total += boundary
            finally:
                temporary.unlink(missing_ok=True)
        for name in ("latest", "latest-view"):
            try:
                with _open_read(directory / name) as pointer:
                    generation = pointer.read(128).decode("ascii")
                if _GENERATION.fullmatch(generation):
                    with _open_read((target / generation).with_suffix(".tdj.commit")):
                        _atomic(target / name, generation.encode())
            except FileNotFoundError:
                pass  # A newly started source generation will copy next time.
    return dict(copied=copied, skipped=skipped, bytes=total)


def mirror_main(argv=None):
    parser = argparse.ArgumentParser(description=mirror_journals.__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(mirror_journals(args.root, args.destination)), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--generation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--export", type=Path, help="write raw recorded ANSI to a new file")
    args = parser.parse_args(argv)
    directory = journal_directory(args.root, args.session_id)
    if args.list:
        print(json.dumps(sorted(p.name for p in directory.iterdir() if _GENERATION.fullmatch(p.name))))
        return 0
    output = os.fdopen(os.open(args.export, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") if args.export else None
    try:
        offset, generation = len(MAGIC), args.generation
        while True:
            page = read_page(args.root, args.session_id, generation, offset)
            for event in page["events"]:
                if output:
                    output.write(bytes.fromhex(event["data_hex"]))
                else:
                    print(json.dumps(event))
            if page["complete"]:
                if page["truncated_tail"]:
                    print("Incomplete final frame ignored; earlier records retained.", file=sys.stderr)
                break
            offset, generation = page["next_offset"], page["generation"]
    finally:
        if output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
