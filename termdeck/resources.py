from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Sequence

from .models import ResourceSnapshot

_PROC = "/proc"
_CLK_TCK = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# Prefer the fast /proc/<pid>/task/<tid>/children walk. Full /proc scans are only
# a fallback for kernels without that interface and are cached briefly.
_CHILDREN_CACHE_TTL = 0.75
_children_cache: tuple[float, dict[int, list[int]]] | None = None
_children_file_supported: bool | None = None

# Automatic crash replay must stay narrower than ordinary user-entered shell
# commands. These flags do not carry prompts, paths, config values, tokens, or
# other user data. Value-bearing options and positional arguments are rejected
# as a whole rather than partially replayed with changed semantics.
_CODEX_RESTART_FLAGS = frozenset(
    {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--full-auto",
        "--no-alt-screen",
        "--oss",
        "--search",
        "--strict-config",
        "--yolo",
    }
)


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def _process_argv(pid: int) -> tuple[str, ...] | None:
    """Read one bounded argv vector from proc without consulting shell text."""

    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as handle:
            data = handle.read(65537)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if not data or len(data) > 65536:
        return None
    return tuple(
        part.decode("utf-8", errors="replace")
        for part in data.rstrip(b"\0").split(b"\0")
        if part
    )


def _process_cwd(pid: int) -> str | None:
    """Read one process cwd while it is still live and resolvable."""

    try:
        directory = Path(os.readlink(f"{_PROC}/{pid}/cwd"))
        if not directory.is_absolute() or not directory.is_dir():
            return None
        return str(directory.resolve())
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def _canonical_codex_session_id(value: object) -> str | None:
    """Return one canonical UUID, rejecting names and malformed identifiers."""

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _primary_codex_session_id(pid: int) -> str | None:
    """Find the primary Codex TUI session UUID held open by one process.

    A Codex process can own rollout files for its primary conversation and for
    several spawned subagents. The primary rollout metadata uses ``source``
    ``"cli"``; subagent metadata uses a structured source object. Reading only
    the bounded first JSONL record avoids inspecting prompts or transcript data.
    """

    proc_cwd = _process_cwd(pid)
    candidates: list[tuple[int, int, str]] = []
    try:
        fd_names = os.listdir(f"{_PROC}/{pid}/fd")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None

    for fd_name in fd_names:
        try:
            target = os.readlink(f"{_PROC}/{pid}/fd/{fd_name}")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if "/sessions/" not in target or not target.endswith(".jsonl"):
            continue
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline(65537)
            if not first_line or len(first_line) > 65536:
                continue
            payload = json.loads(first_line).get("payload", {})
            if not isinstance(payload, dict) or payload.get("source") != "cli":
                continue
            session_id = _canonical_codex_session_id(payload.get("id"))
            if session_id is None:
                continue
            metadata_cwd = payload.get("cwd")
            rank = 0 if proc_cwd and metadata_cwd == proc_cwd else 1
            try:
                modified_ns = os.stat(target).st_mtime_ns
            except OSError:
                modified_ns = 0
            candidates.append((rank, -modified_ns, session_id))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue

    if not candidates:
        return None
    candidates.sort()
    best_rank = candidates[0][0]
    best = [row for row in candidates if row[0] == best_rank]
    # The newest primary rollout is deterministic if a process briefly retains
    # more than one primary descriptor during a resume transition.
    return best[0][2]


def safe_codex_recovery_command(
    argv: Sequence[str],
    *,
    session_id: str | None = None,
) -> str | None:
    """Return a canonical restart-safe Codex command, or reject the argv.

    Only value-free launch flags and one exact UUID are persisted. Positional
    prompts, names, config overrides, paths, remote addresses, and unknown
    options are rejected rather than replayed with changed or unsafe semantics.
    """

    if not argv or Path(str(argv[0])).name.casefold() != "codex":
        return None
    args = [str(part) for part in argv[1:]]
    resume_id: str | None = None
    if "resume" in args:
        resume_index = args.index("resume")
        launch_flags = args[:resume_index]
        resume_args = args[resume_index + 1 :]
        if len(resume_args) != 1:
            return None
        resume_id = _canonical_codex_session_id(resume_args[0])
        if resume_id is None:
            return None
    else:
        launch_flags = args

    if any(part not in _CODEX_RESTART_FLAGS for part in launch_flags):
        return None

    observed_id = _canonical_codex_session_id(session_id) if session_id else None
    if resume_id and observed_id and resume_id != observed_id:
        return None
    exact_id = resume_id or observed_id
    command = ["codex", *launch_flags]
    if exact_id:
        command.extend(["resume", exact_id])
    return shlex.join(command)


def observe_recovery_metadata(
    root_pid: int,
) -> tuple[bool, str | None, str | None]:
    """Observe a safe resumable command and its cwd below a managed pane.

    The boolean distinguishes an idle, readable process tree from a vanished
    or unreadable one. Callers may clear a stale snapshot only for the former.
    Walking descendants handles terminal wrappers whose tmux pane process is
    not the shell or foreground TUI itself.
    """

    pids = process_tree(root_pid)
    if not pids:
        return False, None, None
    observed = False
    weaker_codex: tuple[str, str | None] | None = None
    for pid in pids:
        argv = _process_argv(pid)
        if argv is None:
            continue
        observed = True
        session_id = _primary_codex_session_id(pid)
        command = safe_codex_recovery_command(argv, session_id=session_id)
        if command is not None:
            cwd = _process_cwd(pid)
            if session_id is not None:
                # Contained Codex deliberately enters a fresh session with
                # bubblewrap, so the native process has no host-side controlling
                # TTY. Its open primary rollout (source="cli") is stronger
                # identity evidence than host TTY ownership and already excludes
                # structured subagent rollouts.
                return True, command, cwd
            stat = _parse_stat(pid)
            if not stat:
                continue
            # Weaker Node/containment launchers do not own the primary rollout.
            # Require host foreground-TTY ownership before retaining one as a
            # fallback so background launchers are never replayed automatically.
            if int(stat.get("tty_nr", 0)) == 0 or int(
                stat.get("pgrp", -1)
            ) != int(stat.get("tpgid", -2)):
                continue
            if weaker_codex is None:
                weaker_codex = (command, cwd)
    if weaker_codex is not None:
        return True, weaker_codex[0], weaker_codex[1]
    return observed, None, None


def observe_recovery_command(root_pid: int) -> tuple[bool, str | None]:
    """Compatibility wrapper returning only the observed recovery command."""

    observed, command, _cwd = observe_recovery_metadata(root_pid)
    return observed, command


def _parse_stat(pid: int) -> dict[str, int | str] | None:
    text = _read_text(f"{_PROC}/{pid}/stat")
    if not text or ")" not in text:
        return None
    try:
        before, rest_text = text.rsplit(")", 1)
        rest = rest_text.strip().split()
        if len(rest) < 20:
            return None
        return {
            "pid": int(before.split("(", 1)[0].strip()),
            "comm": before.split("(", 1)[1],
            "state": rest[0],
            "ppid": int(rest[1]),
            "pgrp": int(rest[2]),
            "tty_nr": int(rest[4]),
            "tpgid": int(rest[5]),
            "utime": int(rest[11]),
            "stime": int(rest[12]),
            "starttime": int(rest[19]),
        }
    except (IndexError, ValueError):
        return None


def _rss_bytes(pid: int) -> int:
    text = _read_text(f"{_PROC}/{pid}/statm")
    if not text:
        return 0
    try:
        resident_pages = int(text.split()[1])
        return resident_pages * _PAGE_SIZE
    except (IndexError, ValueError):
        return 0


def _total_jiffies() -> int:
    text = _read_text(f"{_PROC}/stat")
    if not text:
        return 0
    for line in text.splitlines():
        if line.startswith("cpu "):
            return sum(int(part) for part in line.split()[1:] if part.isdigit())
    return 0


def _boot_time() -> int:
    text = _read_text(f"{_PROC}/stat")
    if not text:
        return int(time.time())
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                break
    return int(time.time())


def _pid_alive(pid: int) -> bool:
    return os.path.exists(f"{_PROC}/{pid}")


def _direct_children_via_proc(pid: int) -> list[int] | None:
    """Return direct child PIDs using /proc task children files, or None if unsupported."""

    global _children_file_supported
    task_dir = f"{_PROC}/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return [] if _children_file_supported else None

    children: list[int] = []
    saw_children_file = False
    for tid in tids:
        path = f"{task_dir}/{tid}/children"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                data = handle.read().strip()
            saw_children_file = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if not data:
            continue
        for part in data.split():
            try:
                children.append(int(part))
            except ValueError:
                continue
    if saw_children_file:
        _children_file_supported = True
        return children
    if _children_file_supported is False:
        return None
    # First miss may mean the kernel lacks the interface.
    _children_file_supported = False
    return None


def _children_map_full_scan() -> dict[int, list[int]]:
    """Scan /proc once. Expensive — only used as fallback and cached briefly."""

    global _children_cache
    now = time.monotonic()
    if _children_cache is not None and (now - _children_cache[0]) < _CHILDREN_CACHE_TTL:
        return _children_cache[1]

    children: dict[int, list[int]] = defaultdict(list)
    try:
        with os.scandir(_PROC) as entries:
            for entry in entries:
                name = entry.name
                if not name.isdigit():
                    continue
                stat = _parse_stat(int(name))
                if stat:
                    children[int(stat["ppid"])].append(int(stat["pid"]))
    except OSError:
        pass
    mapping = dict(children)
    _children_cache = (now, mapping)
    return mapping


def process_tree(root_pid: int) -> list[int]:
    """Return root_pid and live descendants.

    Fast path walks /proc/<pid>/task/<tid>/children (O(descendants)).
    Fallback builds a full ppid map once and reuses it for ~750ms.

    Behaviour matches the original full-scan walker: missing/invalid roots
    yield ``[]``, and the root is included when it has a readable ``stat``.
    """

    if not root_pid or _parse_stat(root_pid) is None:
        return []

    # Fast path: walk only the descendant cone.
    first = _direct_children_via_proc(root_pid)
    if first is not None:
        seen: set[int] = set()
        order: list[int] = []
        queue: deque[int] = deque([root_pid])
        while queue:
            pid = queue.popleft()
            if pid in seen:
                continue
            # Keep the same liveness rule as the full scan: only include PIDs
            # that still have a readable /proc/<pid>/stat (except we already
            # validated the root above).
            if pid != root_pid and _parse_stat(pid) is None:
                continue
            seen.add(pid)
            order.append(pid)
            kids = _direct_children_via_proc(pid)
            if kids:
                queue.extend(kids)
        return order

    # Slow fallback for ancient kernels: one cached full scan.
    children = _children_map_full_scan()
    seen = set()
    order = []
    queue = deque([root_pid])
    while queue:
        pid = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        order.append(pid)
        queue.extend(children.get(pid, []))
    return order


class ResourceSampler:
    """Linux /proc sampler with no third-party dependency.

    CPU percentage is calculated like top: 100% roughly means one full CPU core.
    """

    def __init__(
        self,
        *,
        snapshot_cache_ttl: float = 0.75,
        max_cache_entries: int = 4096,
    ) -> None:
        self._previous: dict[int, tuple[int, int]] = {}
        self._boot_time = _boot_time()
        self._cpu_count = max(1, os.cpu_count() or 1)
        self._jiffies_cache: tuple[float, int] | None = None
        self.snapshot_cache_ttl = max(0.0, float(snapshot_cache_ttl))
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._snapshot_cache: dict[int, tuple[float, ResourceSnapshot]] = {}
        self._bulk_refresh_cursor = 0

    def invalidate_cache(self, root_pid: int | None = None) -> None:
        """Drop cached resource snapshots after process-state changes."""
        if root_pid is None:
            self._snapshot_cache.clear()
            return
        self._snapshot_cache.pop(int(root_pid), None)

    def _cached_total_jiffies(self) -> int:
        now = time.monotonic()
        if self._jiffies_cache is not None and (now - self._jiffies_cache[0]) < 0.05:
            return self._jiffies_cache[1]
        value = _total_jiffies()
        self._jiffies_cache = (now, value)
        return value

    def _prune_caches(self) -> None:
        """Bound PID churn without evicting the whole working set at once."""

        while len(self._snapshot_cache) > self.max_cache_entries:
            oldest = next(iter(self._snapshot_cache))
            self._snapshot_cache.pop(oldest, None)
            self._previous.pop(oldest, None)
        while len(self._previous) > self.max_cache_entries:
            oldest = next(iter(self._previous))
            self._previous.pop(oldest, None)
            self._snapshot_cache.pop(oldest, None)

    def snapshot(self, root_pid: int | None) -> ResourceSnapshot:
        if not root_pid:
            return ResourceSnapshot(pid=None, exists=False)

        now = time.monotonic()
        if self.snapshot_cache_ttl:
            cached = self._snapshot_cache.get(int(root_pid))
            if cached is not None and now < cached[0]:
                self._snapshot_cache.pop(int(root_pid), None)
                self._snapshot_cache[int(root_pid)] = cached
                return cached[1]

        pids = process_tree(root_pid)
        if not pids:
            snap = ResourceSnapshot(pid=root_pid, exists=False)
            self._previous.pop(int(root_pid), None)
            if self.snapshot_cache_ttl:
                self._snapshot_cache.pop(int(root_pid), None)
                self._snapshot_cache[int(root_pid)] = (now + self.snapshot_cache_ttl, snap)
            self._prune_caches()
            return snap

        total_proc_jiffies = 0
        memory = 0
        state = "unknown"
        root_start = None

        for pid in pids:
            stat = _parse_stat(pid)
            if not stat:
                continue
            total_proc_jiffies += int(stat["utime"]) + int(stat["stime"])
            memory += _rss_bytes(pid)
            if pid == root_pid:
                state = str(stat["state"])
                root_start = int(stat["starttime"])

        total_jiffies = self._cached_total_jiffies()
        cpu_percent = 0.0
        # Reinsert to keep dict order as a tiny LRU for the bounded churn cache.
        previous = self._previous.pop(root_pid, None)
        if previous and total_jiffies > previous[1]:
            proc_delta = max(0, total_proc_jiffies - previous[0])
            total_delta = max(1, total_jiffies - previous[1])
            cpu_percent = (proc_delta / total_delta) * 100.0 * self._cpu_count
        self._previous[root_pid] = (total_proc_jiffies, total_jiffies)

        runtime = 0.0
        if root_start is not None:
            start_epoch = self._boot_time + (root_start / _CLK_TCK)
            runtime = max(0.0, time.time() - start_epoch)

        snap = ResourceSnapshot(
            pid=root_pid,
            exists=True,
            state=state,
            cpu_percent=cpu_percent,
            memory_bytes=memory,
            runtime_seconds=runtime,
            process_count=len(pids),
        )
        if self.snapshot_cache_ttl:
            self._snapshot_cache.pop(int(root_pid), None)
            self._snapshot_cache[int(root_pid)] = (now + self.snapshot_cache_ttl, snap)
        self._prune_caches()
        return snap

    def snapshots(
        self,
        root_pids: list[int | None],
        *,
        refresh_limit: int | None = None,
    ) -> list[ResourceSnapshot]:
        """Sample many roots with an optional round-robin refresh budget.

        A GTK refresh over thousands of agents must not synchronously walk every
        process tree. Fresh cache entries are always reused. At most
        ``refresh_limit`` stale/missing entries are sampled; the rest retain a
        stale snapshot or a lightweight "known by the mux, sample pending"
        placeholder until a later round.
        """

        if refresh_limit is None:
            return [self.snapshot(pid) for pid in root_pids]

        clean_limit = max(0, int(refresh_limit))
        now = time.monotonic()
        unique_pids = list(dict.fromkeys(int(pid) for pid in root_pids if pid))
        chosen: set[int] = set()
        if unique_pids and clean_limit:
            index = self._bulk_refresh_cursor % len(unique_pids)
            inspected = 0
            while inspected < len(unique_pids) and len(chosen) < clean_limit:
                pid = unique_pids[index]
                cached = self._snapshot_cache.get(pid)
                if cached is None or now >= cached[0]:
                    chosen.add(pid)
                index = (index + 1) % len(unique_pids)
                inspected += 1
            self._bulk_refresh_cursor = index

        refreshed = {pid: self.snapshot(pid) for pid in chosen}
        results: list[ResourceSnapshot] = []
        for root_pid in root_pids:
            if not root_pid:
                results.append(ResourceSnapshot(pid=None, exists=False))
                continue
            pid = int(root_pid)
            if pid in refreshed:
                results.append(refreshed[pid])
                continue
            cached = self._snapshot_cache.get(pid)
            if cached is not None:
                results.append(cached[1])
                continue
            # pane_pids supplied this PID, so represent it as live-but-pending
            # instead of falsely marking the remembered session missing.
            results.append(ResourceSnapshot(pid=pid, exists=True, state="unknown"))
        return results


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"
