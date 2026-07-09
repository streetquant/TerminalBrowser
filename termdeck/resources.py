from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from .models import ResourceSnapshot

_PROC = "/proc"
_CLK_TCK = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# Prefer the fast /proc/<pid>/task/<tid>/children walk. Full /proc scans are only
# a fallback for kernels without that interface and are cached briefly.
_CHILDREN_CACHE_TTL = 0.75
_children_cache: tuple[float, dict[int, list[int]]] | None = None
_children_file_supported: bool | None = None


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


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

    def __init__(self, *, snapshot_cache_ttl: float = 0.75) -> None:
        self._previous: dict[int, tuple[int, int]] = {}
        self._boot_time = _boot_time()
        self._cpu_count = max(1, os.cpu_count() or 1)
        self._jiffies_cache: tuple[float, int] | None = None
        self.snapshot_cache_ttl = max(0.0, float(snapshot_cache_ttl))
        self._snapshot_cache: dict[int, tuple[float, ResourceSnapshot]] = {}

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

    def snapshot(self, root_pid: int | None) -> ResourceSnapshot:
        if not root_pid:
            return ResourceSnapshot(pid=None, exists=False)

        now = time.monotonic()
        if self.snapshot_cache_ttl:
            cached = self._snapshot_cache.get(int(root_pid))
            if cached is not None and now < cached[0]:
                return cached[1]

        pids = process_tree(root_pid)
        if not pids:
            return ResourceSnapshot(pid=root_pid, exists=False)

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
        previous = self._previous.get(root_pid)
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
            self._snapshot_cache[int(root_pid)] = (now + self.snapshot_cache_ttl, snap)
        return snap

    def snapshots(self, root_pids: list[int | None]) -> list[ResourceSnapshot]:
        """Sample many roots efficiently (shared jiffies cache)."""

        return [self.snapshot(pid) for pid in root_pids]


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
