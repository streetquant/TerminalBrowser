"""Bounded propagation of terminal resize notifications across containment.

TermDeck's tmux control client owns terminal geometry, while the private Codex
launcher places the actual TUI behind a shell/helper chain and
``bwrap --new-session``. The kernel then sends PTY ``SIGWINCH`` only to the
outer pane process group. This module starts from one exact pane-root PID,
proves the production launcher topology and private authentication identity,
and forwards the resize without scanning unrelated processes or emitting
terminal input.
"""

from __future__ import annotations

import errno
import os
import re
import select
import signal
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_MAX_PROC_FILE_BYTES = 64 * 1024
_MAX_ENVIRON_BYTES = 1024 * 1024
_DEFAULT_MAX_PROCESSES = 512
_DEFAULT_MAX_DEPTH = 32
_PRIVATE_ENV_KEYS = (
    b"CODEX_HOME",
    b"TERMDECK_AUTH_SCOPE_ID",
    b"CODEX_TERMDECK_GATE_ROOT",
)
_AUTH_SCOPE_PATTERN = re.compile(r"auth-[A-Za-z0-9_-]{1,240}")


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable-enough procfs identity used for one guarded signal transaction."""

    pid: int
    ppid: int
    pgrp: int
    session: int
    start_ticks: int
    uid: int
    comm: str


@dataclass(frozen=True)
class PrivateLaunchTuple:
    """Closed TermDeck identity carried by one private Codex process."""

    codex_home: str
    auth_scope_id: str
    gate_root: str


@dataclass(frozen=True)
class ResizeSignalResult:
    """Outcome of one best-effort, fail-closed resize propagation."""

    signalled: bool
    reason: str
    root_pid: int
    target_pid: int | None = None
    target_pgrp: int | None = None
    scanned_processes: int = 0
    witness_pid: int | None = None
    signal_method: str = "none"


@dataclass(frozen=True)
class _Candidate:
    root: ProcessIdentity
    launcher_chain: tuple[ProcessIdentity, ...]
    parent: ProcessIdentity
    target: ProcessIdentity
    codex_witness: ProcessIdentity
    private_tuple: PrivateLaunchTuple
    scanned_processes: int


class _ProcReadError(RuntimeError):
    """A bounded procfs read or parse failed."""


class _PidfdUnsupported(RuntimeError):
    """The runtime has no usable pidfd signal surface."""


class _PidfdCommitError(RuntimeError):
    """A pidfd commit failed without safe compatibility authority."""


def _bounded_read(path: Path, *, maximum_bytes: int = _MAX_PROC_FILE_BYTES) -> bytes:
    if maximum_bytes < 1:
        raise _ProcReadError("procfs read limit must be positive")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _ProcReadError(f"cannot open {path}") from exc
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = maximum_bytes + 1 - total
            if remaining <= 0:
                raise _ProcReadError(
                    f"procfs file exceeds {maximum_bytes} bytes: {path}"
                )
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise _ProcReadError(
                    f"procfs file exceeds {maximum_bytes} bytes: {path}"
                )
        return b"".join(chunks)
    except OSError as exc:
        raise _ProcReadError(f"cannot read {path}") from exc
    finally:
        os.close(fd)


def _process_identity(pid: int, *, proc_root: Path) -> ProcessIdentity:
    if isinstance(pid, bool) or int(pid) <= 1:
        raise _ProcReadError("process PID must be an integer greater than one")
    pid = int(pid)
    directory = proc_root / str(pid)
    try:
        metadata = directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise _ProcReadError(f"cannot inspect process directory {directory}") from exc
    raw = _bounded_read(directory / "stat").decode("latin-1")
    close = raw.rfind(") ")
    open_paren = raw.find("(")
    if open_paren < 0 or close <= open_paren:
        raise _ProcReadError(f"malformed process stat for PID {pid}")
    comm = raw[open_paren + 1 : close]
    fields = raw[close + 2 :].split()
    # After the comm field, fields[0] is state (field 3) and fields[19] is
    # starttime (field 22). Requiring the full prefix prevents shifted parses.
    if len(fields) < 20:
        raise _ProcReadError(f"truncated process stat for PID {pid}")
    try:
        ppid = int(fields[1])
        pgrp = int(fields[2])
        session_id = int(fields[3])
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise _ProcReadError(f"non-numeric process stat for PID {pid}") from exc
    if min(ppid, pgrp, session_id, start_ticks) < 0:
        raise _ProcReadError(f"invalid negative process identity for PID {pid}")
    return ProcessIdentity(
        pid=pid,
        ppid=ppid,
        pgrp=pgrp,
        session=session_id,
        start_ticks=start_ticks,
        uid=int(metadata.st_uid),
        comm=comm,
    )


def _children(pid: int, *, proc_root: Path) -> tuple[int, ...]:
    raw = _bounded_read(proc_root / str(pid) / "task" / str(pid) / "children")
    children: list[int] = []
    for token in raw.split():
        try:
            child = int(token)
        except ValueError as exc:
            raise _ProcReadError(f"malformed child PID below {pid}") from exc
        if child <= 1:
            raise _ProcReadError(f"invalid child PID below {pid}")
        children.append(child)
    return tuple(children)


def _same_process(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return (
        left.pid == right.pid
        and left.start_ticks == right.start_ticks
        and left.uid == right.uid
        and left.ppid == right.ppid
        and left.pgrp == right.pgrp
        and left.session == right.session
        and left.comm == right.comm
    )


def _is_real_procfs(proc_root: Path) -> bool:
    """Return whether *proc_root* names the host's live procfs mount."""

    try:
        return proc_root.samefile(Path("/proc"))
    except OSError:
        return False


def _open_witness_pidfd(
    pid: int,
    *,
    proc_root: Path,
    opener: Callable[[int, int], int] | None = None,
    sender: Callable[[int, int, object | None, int], None] | None = None,
) -> tuple[int, Callable[[int, int, object | None, int], None]]:
    """Open one kernel identity for the exact private Codex witness.

    Production defaults are available only for the live host procfs. Tests may
    inject an opener/sender pair for a synthetic proc tree. Capability absence
    or ``ENOSYS`` is the sole numeric-group fallback authority; lookup,
    permission, and unexpected failures remain fail closed.
    """

    injected = opener is not None or sender is not None
    if injected and (opener is None or sender is None):
        raise _PidfdCommitError("pidfd test surface is incomplete")
    if not injected:
        if not _is_real_procfs(proc_root):
            raise _PidfdUnsupported("pidfds are disabled for a synthetic proc root")
        opener = getattr(os, "pidfd_open", None)
        sender = getattr(signal, "pidfd_send_signal", None)
        if not callable(opener) or not callable(sender):
            raise _PidfdUnsupported("the Python runtime has no pidfd signal surface")
    assert opener is not None
    assert sender is not None
    try:
        descriptor = opener(int(pid), 0)
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            raise _PidfdUnsupported("the kernel has no pidfd_open syscall") from exc
        if exc.errno == errno.ESRCH:
            raise _PidfdCommitError("Codex witness disappeared before pidfd open") from exc
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise _PidfdCommitError("permission denied opening Codex witness pidfd") from exc
        raise _PidfdCommitError("operating-system error opening Codex witness pidfd") from exc
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
        raise _PidfdCommitError("pidfd_open returned an invalid descriptor")
    return descriptor, sender


def _pidfd_has_exited(descriptor: int) -> bool:
    """Poll one pidfd without waiting; readability means the task exited."""

    try:
        poller = select.poll()
        poller.register(
            int(descriptor),
            select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL,
        )
        events = poller.poll(0)
    except (OSError, ValueError) as exc:
        raise _PidfdCommitError("Codex witness pidfd liveness check failed") from exc
    for _fd, mask in events:
        if mask & select.POLLNVAL:
            raise _PidfdCommitError("Codex witness pidfd became invalid")
    return bool(events)


def _canonical_absolute_path(value: str) -> bool:
    if not value or "\x00" in value or len(value.encode("utf-8")) > 4096:
        return False
    if not os.path.isabs(value) or value == os.path.sep:
        return False
    return os.path.normpath(value) == value


def private_launch_tuple_for_scope(
    codex_home: str,
    auth_scope_id: str,
) -> PrivateLaunchTuple:
    """Construct the exact private tuple expected by one persistent row.

    This is public so the GTK/backend callback can bind process evidence to the
    durable TermDeck record instead of allowing a process to appoint its own
    authentication scope.
    """

    home = str(codex_home or "").strip()
    scope = str(auth_scope_id or "").strip()
    if not _canonical_absolute_path(home):
        raise ValueError("CODEX_HOME must be one normalized absolute path")
    if not _AUTH_SCOPE_PATTERN.fullmatch(scope):
        raise ValueError("TERMDECK_AUTH_SCOPE_ID is invalid")
    if Path(home).name != scope:
        raise ValueError("CODEX_HOME basename does not match its auth scope")
    gate_root = str(Path(home) / ".termdeck-codex-lifetime")
    if not _canonical_absolute_path(gate_root):
        raise ValueError("CODEX_TERMDECK_GATE_ROOT is invalid")
    return PrivateLaunchTuple(home, scope, gate_root)


def _private_launch_tuple(pid: int, *, proc_root: Path) -> PrivateLaunchTuple:
    """Read a bounded, duplicate-free private tuple from procfs environ."""

    raw = _bounded_read(
        proc_root / str(pid) / "environ",
        maximum_bytes=_MAX_ENVIRON_BYTES,
    )
    selected: dict[bytes, bytes] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"=")
        if not separator or key not in _PRIVATE_ENV_KEYS:
            continue
        if key in selected:
            raise _ProcReadError("private launch environment has a duplicate key")
        selected[key] = value
    if set(selected) != set(_PRIVATE_ENV_KEYS):
        raise _ProcReadError("private launch environment is incomplete")
    try:
        home = selected[b"CODEX_HOME"].decode("utf-8", "strict")
        scope = selected[b"TERMDECK_AUTH_SCOPE_ID"].decode("utf-8", "strict")
        gate_root = selected[b"CODEX_TERMDECK_GATE_ROOT"].decode(
            "utf-8", "strict"
        )
    except UnicodeError as exc:
        raise _ProcReadError("private launch environment is not UTF-8") from exc
    try:
        expected = private_launch_tuple_for_scope(home, scope)
    except ValueError as exc:
        raise _ProcReadError("private launch environment is inconsistent") from exc
    if gate_root != expected.gate_root:
        raise _ProcReadError("private launch gate root is inconsistent")
    return expected


def _codex_witness_in_session(
    target: ProcessIdentity,
    *,
    proc_root: Path,
    expected_uid: int,
    expected_private_tuple: PrivateLaunchTuple | None,
    maximum_processes: int,
    maximum_depth: int,
) -> tuple[
    ProcessIdentity | None,
    PrivateLaunchTuple | None,
    str,
    int,
]:
    """Return the unique private Codex witness in one inner session."""

    queue: deque[tuple[ProcessIdentity, int]] = deque([(target, 0)])
    visited = {target.pid}
    scanned = 0
    matches: list[tuple[ProcessIdentity, PrivateLaunchTuple]] = []
    while queue:
        parent, depth = queue.popleft()
        if depth >= maximum_depth:
            return None, None, "process-tree depth bound reached", scanned
        try:
            child_pids = _children(parent.pid, proc_root=proc_root)
        except _ProcReadError:
            continue
        for child_pid in child_pids:
            if child_pid in visited:
                continue
            if scanned >= maximum_processes:
                return None, None, "process-tree node bound reached", scanned
            visited.add(child_pid)
            scanned += 1
            try:
                child = _process_identity(child_pid, proc_root=proc_root)
            except _ProcReadError:
                continue
            if child.uid != expected_uid or child.ppid != parent.pid:
                continue
            if child.session != target.session:
                # MCP and helper subprocesses can create their own sessions;
                # they are not evidence for signaling this candidate group.
                continue
            if child.comm == "codex":
                try:
                    private_tuple = _private_launch_tuple(
                        child.pid,
                        proc_root=proc_root,
                    )
                except _ProcReadError:
                    private_tuple = None
                if private_tuple is not None and (
                    expected_private_tuple is None
                    or private_tuple == expected_private_tuple
                ):
                    matches.append((child, private_tuple))
                    if len(matches) > 1:
                        return (
                            None,
                            None,
                            "ambiguous private Codex witnesses",
                            scanned,
                        )
            queue.append((child, depth + 1))
    if not matches:
        return None, None, "inner bwrap session has no expected private Codex witness", scanned
    witness, private_tuple = matches[0]
    return witness, private_tuple, "private Codex witness found", scanned


def _discover_contained_group(
    root_pid: int,
    *,
    proc_root: Path,
    expected_uid: int,
    expected_private_tuple: PrivateLaunchTuple | None,
    maximum_processes: int,
    maximum_depth: int,
) -> tuple[_Candidate | None, str, int]:
    if maximum_processes < 2 or maximum_depth < 2:
        return None, "invalid traversal bound", 0
    try:
        root = _process_identity(root_pid, proc_root=proc_root)
    except _ProcReadError:
        return None, "pane root is unavailable", 0
    if root.uid != expected_uid:
        return None, "pane root has unexpected ownership", 1

    queue: deque[tuple[ProcessIdentity, tuple[ProcessIdentity, ...], int]] = deque(
        [(root, (), 0)]
    )
    visited = {root.pid}
    scanned = 1
    qualified: list[_Candidate] = []
    saw_inner = False
    last_witness_reason = "no exact private TermDeck containment chain"

    while queue:
        parent, chain, depth = queue.popleft()
        if depth >= maximum_depth:
            return None, "process-tree depth bound reached", scanned
        try:
            child_pids = _children(parent.pid, proc_root=proc_root)
        except _ProcReadError:
            continue
        for child_pid in child_pids:
            if child_pid in visited:
                continue
            if scanned >= maximum_processes:
                return None, "process-tree node bound reached", scanned
            visited.add(child_pid)
            scanned += 1
            try:
                child = _process_identity(child_pid, proc_root=proc_root)
            except _ProcReadError:
                continue
            if child.uid != expected_uid or child.ppid != parent.pid:
                continue
            if child.pgrp != root.pgrp or child.session != root.session:
                continue

            child_chain = (*chain, child)
            if child.comm != "bwrap":
                queue.append((child, child_chain, depth + 1))
                continue

            try:
                inner_pids = _children(child.pid, proc_root=proc_root)
            except _ProcReadError:
                continue
            for inner_pid in inner_pids:
                if inner_pid in visited:
                    continue
                if scanned >= maximum_processes:
                    return None, "process-tree node bound reached", scanned
                visited.add(inner_pid)
                scanned += 1
                try:
                    inner = _process_identity(inner_pid, proc_root=proc_root)
                except _ProcReadError:
                    continue
                if not (
                    inner.uid == expected_uid
                    and inner.ppid == child.pid
                    and inner.comm == "bwrap"
                    and inner.pid == inner.pgrp == inner.session
                    and inner.pgrp != root.pgrp
                ):
                    continue
                saw_inner = True
                remaining = maximum_processes - scanned
                if remaining <= 0:
                    return None, "process-tree node bound reached", scanned
                witness, private_tuple, witness_reason, extra_scanned = (
                    _codex_witness_in_session(
                        inner,
                        proc_root=proc_root,
                        expected_uid=expected_uid,
                        expected_private_tuple=expected_private_tuple,
                        maximum_processes=remaining,
                        maximum_depth=max(1, maximum_depth - depth - 2),
                    )
                )
                scanned += extra_scanned
                last_witness_reason = witness_reason
                if witness is not None and private_tuple is not None:
                    qualified.append(
                        _Candidate(
                            root=root,
                            launcher_chain=child_chain,
                            parent=child,
                            target=inner,
                            codex_witness=witness,
                            private_tuple=private_tuple,
                            scanned_processes=scanned,
                        )
                    )
                    if len(qualified) > 1:
                        return (
                            None,
                            "ambiguous private TermDeck containment chains",
                            scanned,
                        )

    if len(qualified) == 1:
        candidate = qualified[0]
        return candidate, "exact private TermDeck containment chain found", scanned
    if saw_inner:
        return None, last_witness_reason, scanned
    return None, "no exact private TermDeck containment chain", scanned


def _same_candidate(left: _Candidate, right: _Candidate) -> bool:
    return (
        _same_process(left.root, right.root)
        and len(left.launcher_chain) == len(right.launcher_chain)
        and all(
            _same_process(expected, current)
            for expected, current in zip(left.launcher_chain, right.launcher_chain)
        )
        and _same_process(left.parent, right.parent)
        and _same_process(left.target, right.target)
        and _same_process(left.codex_witness, right.codex_witness)
        and left.private_tuple == right.private_tuple
    )


def propagate_terminal_resize(
    root_pid: int,
    *,
    proc_root: Path | str = Path("/proc"),
    expected_uid: int | None = None,
    expected_private_tuple: PrivateLaunchTuple | None = None,
    maximum_processes: int = _DEFAULT_MAX_PROCESSES,
    maximum_depth: int = _DEFAULT_MAX_DEPTH,
    killpg: Callable[[int, int], None] = os.killpg,
    pidfd_open: Callable[[int, int], int] | None = None,
    pidfd_send_signal: Callable[[int, int, object | None, int], None] | None = None,
    pidfd_has_exited: Callable[[int], bool] | None = None,
    close_fd: Callable[[int], None] = os.close,
) -> ResizeSignalResult:
    """Forward one resize to the exact private contained Codex witness.

    On a capable host the witness is pidfd-pinned before the complete graph and
    tuple are revalidated, then receives ``SIGWINCH`` through that descriptor.
    The existing fully revalidated process-group path is retained only for
    genuine capability absence or deterministic synthetic-proc tests.
    """

    proc_root = Path(proc_root)
    uid = os.geteuid() if expected_uid is None else int(expected_uid)
    candidate, reason, scanned = _discover_contained_group(
        int(root_pid),
        proc_root=proc_root,
        expected_uid=uid,
        expected_private_tuple=expected_private_tuple,
        maximum_processes=int(maximum_processes),
        maximum_depth=int(maximum_depth),
    )
    if candidate is None:
        return ResizeSignalResult(
            signalled=False,
            reason=reason,
            root_pid=int(root_pid),
            scanned_processes=scanned,
        )

    descriptor: int | None = None
    sender: Callable[[int, int, object | None, int], None] | None = None
    fallback_authorized = False
    current: _Candidate | None = None
    try:
        try:
            descriptor, sender = _open_witness_pidfd(
                candidate.codex_witness.pid,
                proc_root=proc_root,
                opener=pidfd_open,
                sender=pidfd_send_signal,
            )
        except _PidfdUnsupported:
            fallback_authorized = True
        except _PidfdCommitError as exc:
            reason = str(exc)
        else:
            reason = ""

        if descriptor is None and not fallback_authorized:
            return ResizeSignalResult(
                signalled=False,
                reason=reason,
                root_pid=int(root_pid),
                target_pid=candidate.target.pid,
                target_pgrp=candidate.target.pgrp,
                scanned_processes=scanned,
                witness_pid=candidate.codex_witness.pid,
            )

        current, current_reason, current_scanned = _discover_contained_group(
            int(root_pid),
            proc_root=proc_root,
            expected_uid=uid,
            expected_private_tuple=expected_private_tuple,
            maximum_processes=int(maximum_processes),
            maximum_depth=int(maximum_depth),
        )
        scanned = max(scanned, current_scanned)
        if current is None:
            reason = f"containment changed before signal: {current_reason}"
        elif not _same_candidate(candidate, current):
            reason = "contained process graph or private identity changed before signal"
        elif current.target.pgrp in {os.getpgrp(), os.getpid()}:
            reason = "refusing to signal the controller process group"
        else:
            reason = ""

        if not reason and descriptor is not None:
            liveness = pidfd_has_exited or _pidfd_has_exited
            try:
                exited = bool(liveness(descriptor))
            except _PidfdCommitError as exc:
                reason = str(exc)
            except Exception:
                reason = "Codex witness pidfd liveness check failed"
            else:
                if exited:
                    reason = "Codex witness exited before pidfd signal"

        if not reason and descriptor is not None:
            assert sender is not None
            try:
                sender(descriptor, signal.SIGWINCH, None, 0)
            except ProcessLookupError:
                reason = "Codex witness disappeared before pidfd signal"
            except PermissionError:
                reason = "permission denied signaling Codex witness pidfd"
            except OSError:
                reason = "operating-system error signaling Codex witness pidfd"
            else:
                assert current is not None
                return ResizeSignalResult(
                    signalled=True,
                    reason="resize propagated to exact private Codex pidfd",
                    root_pid=int(root_pid),
                    target_pid=current.target.pid,
                    target_pgrp=current.target.pgrp,
                    scanned_processes=scanned,
                    witness_pid=current.codex_witness.pid,
                    signal_method="pidfd-witness",
                )

        if not reason and fallback_authorized:
            assert current is not None
            try:
                killpg(current.target.pgrp, signal.SIGWINCH)
            except ProcessLookupError:
                reason = "contained process group disappeared before signal"
            except PermissionError:
                reason = "permission denied signaling contained process group"
            except OSError:
                reason = "operating-system error signaling contained process group"
            else:
                return ResizeSignalResult(
                    signalled=True,
                    reason="resize propagated through unsupported-pidfd compatibility path",
                    root_pid=int(root_pid),
                    target_pid=current.target.pid,
                    target_pgrp=current.target.pgrp,
                    scanned_processes=scanned,
                    witness_pid=current.codex_witness.pid,
                    signal_method="process-group-fallback",
                )
    finally:
        if descriptor is not None:
            try:
                close_fd(descriptor)
            except OSError:
                pass

    return ResizeSignalResult(
        signalled=False,
        reason=reason,
        root_pid=int(root_pid),
        target_pid=candidate.target.pid,
        target_pgrp=candidate.target.pgrp,
        scanned_processes=scanned,
        witness_pid=candidate.codex_witness.pid,
    )
