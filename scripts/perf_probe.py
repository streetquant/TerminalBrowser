"""Emit repeatable, headless performance baselines for Phase 0 instrumentation.

Runs without tmux, GTK, or a display so the same numbers are reproducible on a
developer laptop and in CI. Baselines live in docs/perf-baselines/.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from termdeck.frame_budget import FrameBudgetMonitor

ProbeResult = dict[str, float | int] | dict[str, str]


def _summarize(samples_ms: list[float]) -> dict[str, float | int]:
    """Use a shared summary so all probes are comparable in CI dashboards."""
    if not samples_ms:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "n": 0}
    ordered = sorted(samples_ms)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": ordered[p95_index],
        "max_ms": ordered[-1],
        "n": len(ordered),
    }


def _time_iterations(iterations: int, operation: Callable[[], None]) -> ProbeResult:
    """Measure each operation independently so tail latency remains visible."""
    samples_ms: list[float] = []
    try:
        for _ in range(iterations):
            started = time.perf_counter_ns()
            operation()
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
    except Exception as exc:  # A baseline tool must report probe failures, not fail CI.
        return {"error": f"{type(exc).__name__}: {exc}"}
    return _summarize(samples_ms)


def _snapshot_build_probe(iterations: int) -> ProbeResult:
    """Time the real task-snapshot builder over synthetic offline records.

    Uses an unavailable backend and a no-op sampler so the measurement isolates
    snapshot construction cost from tmux/procfs variance.
    """
    try:
        from termdeck.models import ResourceSnapshot, SessionRecord
        from termdeck.task_snapshot import build_task_snapshot
    except Exception as exc:  # pragma: no cover - defensive for partial checkouts.
        return {"error": f"{type(exc).__name__}: {exc}"}

    class ProbeBackend:
        """Offline backend so the probe never shells out to tmux."""

        available = False

    class ProbeSampler:
        """No-op sampler so the probe never scans /proc."""

        def snapshot(self, pid: int | None) -> ResourceSnapshot:
            return ResourceSnapshot(pid=pid, exists=False)

    records = [
        SessionRecord(
            id=f"td-probe-{index}",
            name=f"task-{index}",
            tag="dev" if index % 2 else "ai",
            command=f"echo probe-{index}",
            cwd="/tmp",
            tmux_name=f"termdeck_td-probe-{index}",
            created_at="2026-01-01T00:00:00+00:00",
            last_opened_at="2026-01-01T00:00:00+00:00",
            status="running",
            last_pid=10_000 + index,
            window_id=f"win-{index % 8}",
            window_title=f"Window {index % 8}",
            tab_index=index % 12,
            visible=index % 3 != 0,
        )
        for index in range(100)
    ]
    backend = ProbeBackend()
    sampler = ProbeSampler()

    def build() -> None:
        build_task_snapshot(records, backend=backend, sampler=sampler)

    return _time_iterations(iterations, build)


def _frame_budget_overhead_probe(iterations: int) -> ProbeResult:
    """Measure the guard itself, because diagnostic code must stay negligible."""
    monitor = FrameBudgetMonitor()
    return _time_iterations(iterations, monitor.tick)


def _line_buffer_append_probe(iterations: int) -> ProbeResult:
    """Approximate mirrored terminal-output cost without terminal dependencies."""
    line = "synthetic terminal output"

    def append_lines() -> None:
        buffer: deque[str] = deque(maxlen=10_000)
        buffer.extend(line for _ in range(10_000))

    return _time_iterations(iterations, append_lines)


def run_probes(iterations: int) -> dict[str, Any]:
    """Build the complete JSON-ready document so stdout and files always agree."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "probes": {
            "snapshot_build": _snapshot_build_probe(iterations),
            "frame_budget_overhead": _frame_budget_overhead_probe(iterations),
            "line_buffer_append": _line_buffer_append_probe(iterations),
        },
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Keep CLI validation upfront to avoid silently generating misleading data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, help="Also write JSON to this path.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=200,
        help="Number of samples per probe (default: 200).",
    )
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run all probes and always return success for expected probe-level errors."""
    args = _parse_args(argv)
    result = run_probes(args.iterations)
    rendered = json.dumps(result, sort_keys=True, indent=2)
    print(rendered)
    if args.json_out is not None:
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
