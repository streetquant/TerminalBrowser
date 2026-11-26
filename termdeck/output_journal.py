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
import subprocess
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
        self._pointer_published = False
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
                pointer_path = self.directory / self._pointer
                if not self._pointer_published:
                    _atomic(pointer_path, self.generation.encode("ascii"))
                    self._pointer_published = True
                else:
                    other_name = "latest-view" if self._pointer == "latest" else "latest"
                    try:
                        if os.stat(self.directory / other_name).st_mtime_ns >= os.stat(pointer_path).st_mtime_ns:
                            _atomic(pointer_path, self.generation.encode("ascii"))
                    except FileNotFoundError:
                        pass
            except BaseException:
                self._failed = True
                raise
            finally:
                fcntl.flock(self._backup_lock.fileno(), fcntl.LOCK_UN)

    def close(self):
        with self._lock:
            self._stream.close()
            self._backup_lock.close()


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_journal_stream(directory: Path, generation: str):
    path = directory / generation
    try:
        stream = _open_read(path)
        return stream, os.fstat(stream.fileno()).st_size, None
    except FileNotFoundError:
        zst_path = directory / (generation + ".zst")
        compressed = _open_read(zst_path)
        proc = subprocess.Popen(
            ["/usr/bin/zstd", "-dc", "-q"],
            stdin=compressed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        compressed.close()
        return proc.stdout, None, proc


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
    if not value.isdigit():
        raise ValueError("invalid recording commit boundary")
    committed = int(value)
    stream, physical_size, proc = _open_journal_stream(directory, generation)
    try:
        if _read_exact(stream, len(MAGIC)) != MAGIC:
            raise ValueError("invalid terminal recording header")
        if physical_size is not None:
            size = physical_size
            if not len(MAGIC) <= offset <= committed <= size:
                raise ValueError("terminal recording position past committed end")
            truncated = size > committed
            stream.seek(offset)
            pos = offset
        else:
            # Compressed archived stream: validate committed bound and skip forward sequentially.
            if not len(MAGIC) <= offset <= committed:
                raise ValueError("terminal recording position past committed end")
            skip = offset - len(MAGIC)
            while skip > 0:
                chunk = _read_exact(stream, min(65536, skip))
                if not chunk:
                    raise ValueError("truncated compressed recording")
                skip -= len(chunk)
            pos = offset
        while total < limit:
            start = pos
            if start == committed:
                complete = True
                break
            header = _read_exact(stream, min(5, committed - start))
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
            data = _read_exact(stream, length)
            digest = _read_exact(stream, 32)
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
            pos += 5 + length + 32
    finally:
        if stream is not None:
            stream.close()
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
    return dict(generation=generation, next_offset=pos, complete=complete,
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
        if len(arguments) >= 6 and arguments[:3] == ["/usr/bin/python3", "-I", "-B"] and arguments[4] == "--record-output":
            bundle_entry = Path(arguments[3])
            flag_args = arguments[5:]
        elif len(arguments) >= 3 and Path(arguments[0]).name == "termdeck-record-output" and arguments[1] == "--record-output":
            bundle_entry = Path(arguments[0])
            if bundle_entry != bundle_entry.parent / "termdeck-record-output":
                return None
            flag_args = arguments[2:]
        else:
            return None
        flags = dict(zip(flag_args[::2], flag_args[1::2]))
        if any(flags.get(key) != value for key, value in {
                "--root": str(root), "--session-id": session_id, "--pane-id": pane_id,
                "--server-pid": str(server_pid), "--server-start": str(server_start),
                "--token": owner["token"]}.items()):
            return None
        if bundle_entry not in _PIPE_BUNDLES:
            from .startup_publication import verify_bundle
            verify_bundle(bundle_entry.parent)
            _PIPE_BUNDLES.add(bundle_entry)
        return owner
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError):
        return None


def _pipe_input(fd):
    """Drain independently of disk I/O; fail the sink at a finite backlog.

    Closing the input on overflow lets tmux discard this pipe's output buffer.
    Never let a stalled fsync turn our pipe into an unbounded server buffer.
    The last published watermark remains the only advertised durable prefix.
    """
    import queue
    pending = queue.Queue(maxsize=64)  # At most 4 MiB plus one 64 KiB read.
    def collect():
        try:
            while data := os.read(fd, 64 * 1024):
                try:
                    pending.put_nowait(data)
                except queue.Full:
                    os.close(fd)
                    print("terminal recording failed: output backlog exceeded 4 MiB", file=sys.stderr, flush=True)
                    os._exit(1)  # Only this sink; the pane/app stays untouched.
        finally:
            pending.put(None)
    threading.Thread(target=collect, name="terminal-recording-input", daemon=True).start()
    return pending


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
        pending = _pipe_input(0)
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
        _record_reattachment_if_same_pane(
            journal.directory,
            session_id=args.session_id,
            pane_id=args.pane_id,
            pane_pid=args.pane_pid,
            pane_start=args.pane_start,
            server_pid=args.server_pid,
            server_start=args.server_start,
            new_generation=journal.generation,
        )
        owner = dict(session_id=args.session_id, pane_id=args.pane_id,
            pane_pid=args.pane_pid, pane_start=args.pane_start,
            server_pid=args.server_pid, server_start=args.server_start,
            pid=os.getpid(), starttime=own["starttime"], argv=_process_argv(os.getpid()),
            token=args.token, generation=journal.generation)
        _atomic(journal.directory / "pipe-owner.json", json.dumps(owner).encode())
        import queue
        import termios
        geometry = (args.columns, args.rows)
        current_pid, current_start = args.pane_pid, args.pane_start
        last_geometry_check = time.monotonic() - 1.0
        while True:
            eof = False
            try:
                data = pending.get(timeout=1)
            except queue.Empty:
                data = b""
            if data is None:
                eof = True
                data = b""
            elif data:
                chunks = [data]
                total = len(data)
                window_deadline = time.monotonic() + 0.05
                while total < 256 * 1024:
                    remaining = window_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = pending.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if nxt is None:
                        eof = True
                        break
                    if nxt:
                        chunks.append(nxt)
                        total += len(nxt)
                data = b"".join(chunks)
            now = time.monotonic()
            if eof or now - last_geometry_check >= 1.0:
                last_geometry_check = now
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
            if data:
                journal.append(data)
            if eof:
                break
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


def _verify_committed_frames(stream, start_offset: int, boundary: int) -> list[bytes]:
    """Read and validate committed journal frames in [start_offset, boundary)."""
    stream.seek(start_offset)
    chunks: list[bytes] = []
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
        chunks.append(header + payload + digest)
    if stream.tell() != boundary:
        raise ValueError("source recording frame boundary mismatch")
    return chunks


def _try_append_mirror_suffix(path: Path, final: Path, prev_boundary: int, boundary: int) -> int | None:
    """Append newly committed frames [prev_boundary, boundary) when prefix matches."""
    if prev_boundary < len(MAGIC) or prev_boundary >= boundary:
        return None
    if prev_boundary > len(MAGIC) and prev_boundary < len(MAGIC) + 5 + 32:
        return None
    try:
        dst_fd = os.open(final, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(dst_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size < prev_boundary
        ):
            return None
        with _open_read(path) as stream:
            if stream.read(8) != MAGIC or os.fstat(stream.fileno()).st_size < boundary:
                raise ValueError("invalid source recording prefix")
            if os.pread(dst_fd, 8, 0) != MAGIC:
                return None
            if prev_boundary > len(MAGIC):
                stream.seek(prev_boundary - 32)
                src_tail = stream.read(32)
                dst_tail = os.pread(dst_fd, 32, prev_boundary - 32)
                if len(src_tail) != 32 or src_tail != dst_tail:
                    return None
            chunks = _verify_committed_frames(stream, prev_boundary, boundary)
        if info.st_size != prev_boundary:
            os.ftruncate(dst_fd, prev_boundary)
        os.lseek(dst_fd, prev_boundary, os.SEEK_SET)
        for chunk in chunks:
            view = memoryview(chunk)
            written = 0
            while written < len(view):
                written += os.write(dst_fd, view[written:])
        os.fsync(dst_fd)
        return boundary - prev_boundary
    finally:
        os.close(dst_fd)


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
        entries = list(directory.iterdir())
        plain_names = {e.name for e in entries if _GENERATION.fullmatch(e.name)}
        for path in entries:
            if path.name.endswith(".tdj.zst") and _GENERATION.fullmatch(path.name[:-4]):
                gen_name = path.name[:-4]
                if gen_name in plain_names:
                    continue  # Stage-B source still has plain .tdj; handled below.
                try:
                    with _open_read((directory / gen_name).with_suffix(".tdj.commit")) as commit:
                        value = commit.read(32)
                except FileNotFoundError:
                    continue
                if not value.isdigit() or int(value) < len(MAGIC):
                    raise ValueError("invalid source recording watermark")
                boundary = int(value)
                dst_commit = (target / gen_name).with_suffix(".tdj.commit")
                dst_zst = target / path.name
                dst_plain = target / gen_name
                already = False
                try:
                    with _open_read(dst_commit) as existing:
                        prev_raw = existing.read(32)
                    if prev_raw.isdigit() and int(prev_raw) >= boundary:
                        try:
                            with _open_read(dst_zst) as existing_zst:
                                if os.fstat(existing_zst.fileno()).st_size > 0:
                                    already = True
                        except FileNotFoundError:
                            with _open_read(dst_plain) as existing_plain:
                                if os.fstat(existing_plain.fileno()).st_size >= int(prev_raw):
                                    already = True
                except FileNotFoundError:
                    already = False
                if already:
                    skipped += 1
                    continue
                temporary = target / ("." + path.name + "-" + uuid4().hex)
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    zst_bytes = 0
                    with os.fdopen(fd, "wb") as output, _open_read(path) as stream:
                        while True:
                            buf = stream.read(256 * 1024)
                            if not buf:
                                break
                            output.write(buf)
                            zst_bytes += len(buf)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, dst_zst)
                    _sync(target)
                    _atomic(dst_commit, str(boundary).encode())
                    copied += 1
                    total += zst_bytes
                finally:
                    temporary.unlink(missing_ok=True)
                continue

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
            dst_zst = target / (path.name + ".zst")
            prev_boundary: int | None = None
            try:
                with _open_read(final.with_suffix(".tdj.commit")) as existing:
                    previous = existing.read(32)
                if previous.isdigit():
                    prev_val = int(previous)
                    if prev_val >= boundary:
                        try:
                            with _open_read(dst_zst) as existing_zst:
                                if os.fstat(existing_zst.fileno()).st_size > 0:
                                    skipped += 1
                                    continue
                        except FileNotFoundError:
                            pass
                        with _open_read(final) as existing_plain:
                            if os.fstat(existing_plain.fileno()).st_size >= prev_val:
                                skipped += 1
                                continue
                    else:
                        prev_boundary = prev_val
            except FileNotFoundError:
                pass
            if prev_boundary is not None:
                delta = _try_append_mirror_suffix(path, final, prev_boundary, boundary)
                if delta is not None:
                    _atomic(final.with_suffix(".tdj.commit"), str(boundary).encode())
                    copied += 1
                    total += delta
                    continue
            temporary = target / ("." + path.name + "-" + uuid4().hex)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output, _open_read(path) as stream:
                    if stream.read(8) != MAGIC or os.fstat(stream.fileno()).st_size < boundary:
                        raise ValueError("invalid source recording prefix")
                    output.write(MAGIC)
                    for chunk in _verify_committed_frames(stream, len(MAGIC), boundary):
                        output.write(chunk)
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
                        encoded = generation.encode()
                        same_pointer = False
                        try:
                            with _open_read(target / name) as dst_ptr:
                                same_pointer = dst_ptr.read(128) == encoded
                        except FileNotFoundError:
                            same_pointer = False
                        if not same_pointer:
                            _atomic(target / name, encoded)
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


COMPACTION_SCHEMA = "termdeck.compaction-status.v1"
RECORDING_EVENT_SCHEMA = "termdeck.recording-event.v1"
HEALTH_SCHEMA = "termdeck.journal-health.v1"
COMPACTION_EXCLUDED_RESERVED_CPUS = "0-2,7-26,31-47"
_ORPHAN_ZST_TMP = re.compile(r"\.[0-9]{20}-[0-9a-f]{32}\.tdj\.zst-[0-9a-f]{32}\Z")


def list_generations(root: Path, session_id: str) -> list[str]:
    """Return sorted deduplicated `.tdj` generation names across plain and `.zst` files."""
    directory = journal_directory(root, session_id)
    found: set[str] = set()
    for entry in directory.iterdir():
        if entry.is_symlink():
            continue
        name = entry.name[:-4] if entry.name.endswith(".zst") else entry.name
        if _GENERATION.fullmatch(name):
            found.add(name)
    return sorted(found)


def _append_recording_event(directory: Path, payload: dict) -> None:
    line = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    target = directory / "recording-events.jsonl"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("unsafe recording events log")
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _record_reattachment_if_same_pane(
    directory: Path,
    *,
    session_id: str,
    pane_id: str,
    pane_pid: int,
    pane_start: int,
    server_pid: int,
    server_start: int,
    new_generation: str,
) -> None:
    try:
        with _open_read(directory / "pipe-owner.json") as prev_stream:
            prev = json.loads(prev_stream.read(16384))
    except (FileNotFoundError, ValueError, OSError):
        return
    if (
        prev.get("session_id"),
        prev.get("pane_id"),
        prev.get("pane_pid"),
        prev.get("pane_start"),
        prev.get("server_pid"),
        prev.get("server_start"),
    ) != (session_id, pane_id, pane_pid, pane_start, server_pid, server_start):
        return
    prev_gen = prev.get("generation") if isinstance(prev.get("generation"), str) else ""
    prev_token = prev.get("token") if isinstance(prev.get("token"), str) else ""
    prior_reason = "unclean_exit_or_sigkill"
    if prev_token and re.fullmatch(r"[0-9a-f]{32}", prev_token):
        try:
            with _open_read(directory / f"{prev_token}.failed.json") as fail_stream:
                fail_doc = json.loads(fail_stream.read(8192))
            prior_reason = str(fail_doc.get("reason") or fail_doc.get("type") or "recorder_error")
        except (FileNotFoundError, ValueError, OSError):
            pass
    now_ms = time.time_ns() // 1_000_000
    gap_ms = None
    if prev_gen and _GENERATION.fullmatch(prev_gen):
        try:
            commit_stat = (directory / f"{prev_gen}.commit").lstat()
            gap_ms = max(0, now_ms - (commit_stat.st_mtime_ns // 1_000_000))
        except OSError:
            pass
    try:
        _append_recording_event(directory, {
            "schema": RECORDING_EVENT_SCHEMA,
            "event": "sink_reattached",
            "gap_detected": True,
            "session_id": session_id,
            "pane_id": pane_id,
            "previous_generation": prev_gen,
            "new_generation": new_generation,
            "prior_failure_reason": prior_reason,
            "gap_ms": gap_ms,
            "timestamp_ms": now_ms,
        })
    except OSError:
        pass


def _stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sha256_plain_checked(path: Path) -> tuple[str, int, tuple[int, int, int, int, int]]:
    before = path.lstat()
    hasher = hashlib.sha256()
    total = 0
    with _open_read(path) as stream:
        while chunk := stream.read(65536):
            hasher.update(chunk)
            total += len(chunk)
    after = path.lstat()
    fp = _stat_fingerprint(before)
    if fp != _stat_fingerprint(after) or total != before.st_size:
        raise OSError("journal file mutated during hash verification")
    return hasher.hexdigest(), total, fp


def _sha256_zstd_decompressed(zst_path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    compressed = _open_read(zst_path)
    proc = subprocess.Popen(
        ["/usr/bin/zstd", "-dc", "-q"],
        stdin=compressed,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    compressed.close()
    try:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(65536):
            hasher.update(chunk)
            total += len(chunk)
        stderr = proc.stderr.read(4096) if proc.stderr is not None else b""
        rc = proc.wait(timeout=30)
        if rc != 0:
            raise subprocess.CalledProcessError(rc, ["/usr/bin/zstd", "-dc", "-q"], stderr=stderr)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return hasher.hexdigest(), total


def _journal_footprint(source: Path) -> tuple[int, int, int, int, int]:
    plain_count = zst_count = orphan_count = plain_bytes = zst_bytes = 0
    if not source.exists():
        return 0, 0, 0, 0, 0
    for tab in source.iterdir():
        if not _ID.fullmatch(tab.name) or not tab.is_dir() or tab.is_symlink():
            continue
        for item in tab.iterdir():
            if item.is_symlink():
                continue
            try:
                sz = item.lstat().st_size
            except OSError:
                continue
            if _GENERATION.fullmatch(item.name):
                plain_count += 1
                plain_bytes += sz
            elif item.name.endswith(".zst") and _GENERATION.fullmatch(item.name[:-4]):
                zst_count += 1
                zst_bytes += sz
            elif _ORPHAN_ZST_TMP.fullmatch(item.name):
                orphan_count += 1
    return plain_count, zst_count, orphan_count, plain_bytes, zst_bytes


def compact_journals(
    root: Path,
    *,
    keep_recent: int = 8,
    min_age_seconds: int = 3600,
    max_files: int | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Losslessly compress closed historical `.tdj` generations to `.tdj.zst` with SHA-256 verification."""
    root = _directory(root)
    source = root / "journals"
    started_ns = time.time_ns()
    started_mono = time.monotonic()
    lock_path = root / ".compact.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.geteuid() or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise OSError("unsafe compaction lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(
                schema=COMPACTION_SCHEMA,
                status="skipped_locked",
                locked=True,
                interrupted=False,
                compressed=0,
                resumed_unlinks=0,
                orphans_cleaned=0,
                sha256_verified=0,
                skipped=0,
                failed=0,
                bytes_before=0,
                bytes_after=0,
                errors=[],
            )

        compressed = skipped = failed = resumed_unlinks = orphans_cleaned = sha256_verified = 0
        bytes_before = bytes_after = 0
        interrupted = False
        errors: list[dict] = []

        def record_error(session_id: str, generation: str, reason: str) -> None:
            if len(errors) < 32:
                errors.append({"session_id": session_id, "generation": generation, "reason": reason})

        if source.exists():
            cutoff = time.time() - max(0, min_age_seconds)
            for tab in sorted(source.iterdir(), key=lambda p: p.name):
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    break
                if not _ID.fullmatch(tab.name) or not tab.is_dir() or tab.is_symlink():
                    continue
                directory = journal_directory(root, tab.name)
                # Under exclusive .compact.lock, clean orphan temp files from prior crashed runs.
                for item in list(directory.iterdir()):
                    if _ORPHAN_ZST_TMP.fullmatch(item.name) and not item.is_symlink():
                        try:
                            st = item.lstat()
                            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid():
                                item.unlink()
                                orphans_cleaned += 1
                        except OSError:
                            pass

                protected = set()
                for pointer_name in ("latest", "latest-view"):
                    try:
                        with _open_read(directory / pointer_name) as pointer:
                            protected.add(pointer.read(128).decode("ascii").strip())
                    except FileNotFoundError:
                        pass
                try:
                    with _open_read(directory / "pipe-owner.json") as owner_file:
                        owner = json.loads(owner_file.read(16384))
                    gen = owner.get("generation")
                    if isinstance(gen, str):
                        protected.add(gen)
                except (FileNotFoundError, ValueError, OSError):
                    pass

                generations = sorted(
                    (p for p in directory.iterdir() if _GENERATION.fullmatch(p.name) and not p.is_symlink()),
                    key=lambda p: p.name,
                )
                if keep_recent > 0:
                    protected.update(p.name for p in generations[-keep_recent:])
                for path in generations:
                    if stop_event is not None and stop_event.is_set():
                        interrupted = True
                        break
                    if max_files is not None and (compressed + resumed_unlinks) >= max_files:
                        break
                    if path.name in protected:
                        skipped += 1
                        continue
                    commit_path = path.with_suffix(".tdj.commit")
                    try:
                        info = path.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_uid != os.geteuid()
                                or stat.S_IMODE(info.st_mode) != 0o600):
                            skipped += 1
                            continue
                        if info.st_mtime > cutoff:
                            skipped += 1
                            continue
                        with _open_read(commit_path) as commit:
                            cval = commit.read(32)
                        if not cval.isdigit() or int(cval) < len(MAGIC) or info.st_size < int(cval):
                            skipped += 1
                            continue
                    except (FileNotFoundError, OSError):
                        skipped += 1
                        continue

                    final_zst = directory / (path.name + ".zst")
                    # Stage-B crash recovery: if final_zst already exists alongside path,
                    # verify byte-for-byte SHA-256 equivalence before completing unlink.
                    if os.path.lexists(final_zst):
                        try:
                            orig_sha, orig_sz, _ = _sha256_plain_checked(path)
                            zst_sha, zst_decomp_sz = _sha256_zstd_decompressed(final_zst)
                            if zst_sha == orig_sha and zst_decomp_sz == orig_sz:
                                zst_size = final_zst.lstat().st_size
                                _sync(directory)
                                path.unlink()
                                _sync(directory)
                                resumed_unlinks += 1
                                sha256_verified += 1
                                bytes_before += orig_sz
                                bytes_after += zst_size
                                continue
                        except (OSError, subprocess.SubprocessError):
                            pass  # Corrupt or partial final_zst; fall through to re-compress from intact .tdj.

                    temporary = directory / ("." + path.name + ".zst-" + uuid4().hex)
                    try:
                        before_fp = _stat_fingerprint(info)
                        orig_hasher = hashlib.sha256()
                        orig_bytes = 0
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        try:
                            with _open_read(path) as raw_in, os.fdopen(fd, "wb") as raw_out:
                                fd = -1
                                proc = subprocess.Popen(
                                    ["/usr/bin/zstd", "-3", "-q", "--no-progress"],
                                    stdin=subprocess.PIPE,
                                    stdout=raw_out,
                                    stderr=subprocess.PIPE,
                                )
                                try:
                                    assert proc.stdin is not None
                                    while chunk := raw_in.read(65536):
                                        orig_hasher.update(chunk)
                                        orig_bytes += len(chunk)
                                        proc.stdin.write(chunk)
                                    proc.stdin.close()
                                    proc.stdin = None
                                    stderr_out = proc.stderr.read(4096) if proc.stderr is not None else b""
                                    rc = proc.wait(timeout=60)
                                    if rc != 0:
                                        raise subprocess.CalledProcessError(
                                            rc, ["/usr/bin/zstd", "-3", "-q"], stderr=stderr_out
                                        )
                                finally:
                                    if proc.stdin is not None:
                                        proc.stdin.close()
                                    if proc.stderr is not None:
                                        proc.stderr.close()
                                    if proc.poll() is None:
                                        proc.kill()
                                        proc.wait(timeout=5)
                                raw_out.flush()
                                os.fsync(raw_out.fileno())
                            after_info = path.lstat()
                            with _open_read(commit_path) as commit_recheck:
                                cval_after = commit_recheck.read(32)
                            if (_stat_fingerprint(after_info) != before_fp
                                    or orig_bytes != info.st_size
                                    or cval_after != cval):
                                raise OSError("source journal changed during compression")
                            orig_sha = orig_hasher.hexdigest()
                            decomp_sha, decomp_bytes = _sha256_zstd_decompressed(temporary)
                            if decomp_bytes != orig_bytes or decomp_sha != orig_sha:
                                raise ValueError("sha256_mismatch")
                            zst_size = temporary.lstat().st_size
                            os.replace(temporary, final_zst)
                            _sync(directory)
                            path.unlink()
                            _sync(directory)
                            compressed += 1
                            sha256_verified += 1
                            bytes_before += orig_bytes
                            bytes_after += zst_size
                        finally:
                            if fd >= 0:
                                os.close(fd)
                            temporary.unlink(missing_ok=True)
                    except ValueError as exc:
                        failed += 1
                        record_error(tab.name, path.name, str(exc))
                    except (OSError, subprocess.SubprocessError) as exc:
                        failed += 1
                        record_error(tab.name, path.name, type(exc).__name__)
                if max_files is not None and (compressed + resumed_unlinks) >= max_files:
                    break

        _, _, _, plain_bytes, zst_bytes = _journal_footprint(source)
        vfs = os.statvfs(root)
        free_bytes = vfs.f_bavail * vfs.f_frsize
        status = "failed" if failed > 0 else ("interrupted" if interrupted else "ok")
        summary = dict(
            schema=COMPACTION_SCHEMA,
            status=status,
            locked=False,
            interrupted=interrupted,
            started_at_ms=started_ns // 1_000_000,
            finished_at_ms=time.time_ns() // 1_000_000,
            duration_ms=round((time.monotonic() - started_mono) * 1000.0, 2),
            compressed=compressed,
            resumed_unlinks=resumed_unlinks,
            orphans_cleaned=orphans_cleaned,
            sha256_verified=sha256_verified,
            skipped=skipped,
            failed=failed,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            journal_bytes_total=plain_bytes + zst_bytes,
            filesystem_free_bytes=free_bytes,
            errors=errors,
        )
        try:
            _atomic(root / "compaction-status.json", (json.dumps(summary, sort_keys=True) + "\n").encode("utf-8"))
        except OSError:
            pass
        return summary
    finally:
        os.close(lock_fd)


def audit_journal_health(root: Path) -> dict:
    """Summarize recorder failures, reattachments, recording gaps, journal growth, and disk pressure."""
    root = _directory(root)
    source = root / "journals"
    sessions_total = 0
    recorder_failures_total = 0
    failures_by_reason: dict[str, int] = {}
    reattachments_total = 0
    recording_gaps_total = 0
    max_gap_ms = 0
    recent_gaps: list[dict] = []

    if source.exists():
        for tab in sorted(source.iterdir(), key=lambda p: p.name):
            if not _ID.fullmatch(tab.name) or not tab.is_dir() or tab.is_symlink():
                continue
            sessions_total += 1
            directory = journal_directory(root, tab.name)
            for item in directory.iterdir():
                if item.is_symlink() or not item.name.endswith(".failed.json"):
                    continue
                recorder_failures_total += 1
                reason = "unknown"
                try:
                    with _open_read(item) as fail_stream:
                        doc = json.loads(fail_stream.read(8192))
                    reason = str(doc.get("reason") or doc.get("type") or "unknown")
                except (ValueError, OSError):
                    reason = "unreadable_failure_notice"
                failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1

            events_path = directory / "recording-events.jsonl"
            try:
                with _open_read(events_path) as ev_stream:
                    for raw_line in ev_stream:
                        if len(raw_line) > 8192:
                            continue
                        ev = json.loads(raw_line)
                        if ev.get("event") == "sink_reattached":
                            reattachments_total += 1
                        if ev.get("gap_detected"):
                            recording_gaps_total += 1
                            gap_val = ev.get("gap_ms")
                            if isinstance(gap_val, int) and gap_val > max_gap_ms:
                                max_gap_ms = gap_val
                            recent_gaps.append(ev)
            except FileNotFoundError:
                pass
            except (ValueError, OSError):
                pass

    recent_gaps.sort(key=lambda item: int(item.get("timestamp_ms") or 0), reverse=True)
    recent_gaps = recent_gaps[:20]

    plain_count, zst_count, orphan_count, plain_bytes, zst_bytes = _journal_footprint(source)
    vfs = os.statvfs(root)
    total_fs = vfs.f_blocks * vfs.f_frsize
    free_fs = vfs.f_bavail * vfs.f_frsize
    used_pct = round(((total_fs - free_fs) / total_fs) * 100.0, 2) if total_fs > 0 else 0.0
    if free_fs < 10 * (1 << 30) or used_pct >= 97.0:
        disk_pressure = "critical"
    elif free_fs < 25 * (1 << 30) or used_pct >= 95.0:
        disk_pressure = "elevated"
    else:
        disk_pressure = "normal"

    last_compaction = None
    try:
        with _open_read(root / "compaction-status.json") as status_stream:
            last_compaction = json.loads(status_stream.read(65536))
    except (FileNotFoundError, ValueError, OSError):
        pass

    return dict(
        schema=HEALTH_SCHEMA,
        checked_at_ms=time.time_ns() // 1_000_000,
        sessions_total=sessions_total,
        generations_plain=plain_count,
        generations_compressed=zst_count,
        orphan_temp_files=orphan_count,
        plain_bytes=plain_bytes,
        compressed_bytes=zst_bytes,
        journal_bytes_total=plain_bytes + zst_bytes,
        recorder_failures_total=recorder_failures_total,
        failures_by_reason=failures_by_reason,
        reattachments_total=reattachments_total,
        recording_gaps_total=recording_gaps_total,
        max_gap_ms=max_gap_ms,
        recent_gaps=recent_gaps,
        filesystem_total_bytes=total_fs,
        filesystem_free_bytes=free_fs,
        filesystem_used_percent=used_pct,
        disk_pressure=disk_pressure,
        last_compaction=last_compaction,
    )


def render_compaction_systemd_units(
    launcher: Path,
    durability_root: Path,
    *,
    extra_roots: tuple[Path, ...] = (),
    allowed_cpus: str = COMPACTION_EXCLUDED_RESERVED_CPUS,
    keep_recent: int = 8,
    min_age_seconds: int = 3600,
) -> tuple[str, str]:
    """Render user systemd `.service` and `.timer` units for low-priority weekly compaction."""
    def quote(val: object) -> str:
        text = str(val)
        if any(ch in text for ch in ("\n", "\r", "\x00")):
            raise ValueError("unit argument contains control bytes")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    if not re.fullmatch(r"[0-9,-]+", allowed_cpus):
        raise ValueError("invalid AllowedCPUs mask")
    def cmd_for(target_root: Path) -> str:
        return " ".join(quote(arg) for arg in (
            "/usr/bin/python3", "-I", "-B", launcher,
            "--compact-journals",
            "--root", target_root,
            "--keep-recent", str(int(keep_recent)),
            "--min-age-seconds", str(int(min_age_seconds)),
        ))

    exec_lines = [f"ExecStart={cmd_for(durability_root)}"]
    for extra in extra_roots:
        exec_lines.append(f"ExecStart=-{cmd_for(extra)}")
    exec_block = "\n".join(exec_lines)
    service = (
        "[Unit]\n"
        "Description=Weekly low-priority TermDeck output journal compaction\n"
        "After=termdeck-directory-tracker.service\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "UMask=0077\n"
        "Nice=19\n"
        "IOSchedulingClass=idle\n"
        "CPUQuota=50%\n"
        f"AllowedCPUs={allowed_cpus}\n"
        "MemoryMax=1G\n"
        "NoNewPrivileges=yes\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=30s\n"
        f"{exec_block}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run TermDeck output journal compaction weekly during off-hours\n\n"
        "[Timer]\n"
        "OnCalendar=Sun *-*-* 04:15:00\n"
        "Persistent=true\n"
        "AccuracySec=5min\n"
        "RandomizedDelaySec=10min\n"
        "Unit=termdeck-journal-compaction.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def compact_main(argv=None):
    import signal as _signal
    parser = argparse.ArgumentParser(description=compact_journals.__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--keep-recent", type=int, default=8)
    parser.add_argument("--min-age-seconds", type=int, default=3600)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--audit-only", action="store_true",
                        help="inspect journal durability health without running compaction")
    args = parser.parse_args(argv)
    if args.audit_only:
        print(json.dumps(audit_journal_health(args.root)), flush=True)
        return 0
    stop_event = threading.Event()
    previous_handlers = {}
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            previous_handlers[sig] = _signal.signal(sig, lambda *_: stop_event.set())
        except ValueError:
            pass
    try:
        summary = compact_journals(
            args.root,
            keep_recent=args.keep_recent,
            min_age_seconds=args.min_age_seconds,
            max_files=args.max_files,
            stop_event=stop_event,
        )
    finally:
        for sig, handler in previous_handlers.items():
            try:
                _signal.signal(sig, handler)
            except ValueError:
                pass
    print(json.dumps(summary), flush=True)
    return 1 if summary.get("failed", 0) > 0 else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--session-id")
    parser.add_argument("--generation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--compact", action="store_true",
                        help="run non-overlapping SHA-256-verified journal compaction")
    parser.add_argument("--keep-recent", type=int, default=8)
    parser.add_argument("--min-age-seconds", type=int, default=3600)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--audit-health", action="store_true",
                        help="emit JSON durability and compaction health summary")
    parser.add_argument("--export", type=Path, help="write raw recorded ANSI to a new file")
    args = parser.parse_args(argv)
    if args.audit_health:
        print(json.dumps(audit_journal_health(args.root)), flush=True)
        return 0
    if args.compact:
        argv_compact = [
            "--root", str(args.root),
            "--keep-recent", str(args.keep_recent),
            "--min-age-seconds", str(args.min_age_seconds),
        ]
        if args.max_files is not None:
            argv_compact.extend(["--max-files", str(args.max_files)])
        return compact_main(argv_compact)
    if not args.session_id:
        parser.error("--session-id is required unless --audit-health or --compact is specified")
    journal_directory(args.root, args.session_id)
    if args.list:
        print(json.dumps(list_generations(args.root, args.session_id)))
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
