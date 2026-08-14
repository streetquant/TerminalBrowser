"""Small, dependency-free instrumentation for detecting UI-loop stalls.

Keeping this module independent of GTK makes its behaviour cheap to test and
allows callers to collect evidence before deciding whether a UI optimisation is
worth its added complexity.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
import time
from collections.abc import Callable


@dataclass(frozen=True)
class Stall:
    """One over-budget gap, retained so a report names actionable incidents."""

    started_at: float
    duration_ms: float


@dataclass(frozen=True)
class FrameBudgetReport:
    """An immutable summary, so reporting cannot accidentally alter monitoring."""

    ticks: int
    stalls: int
    worst_ms: float
    p95_ms: float
    recent_stalls: tuple[Stall, ...]


class FrameBudgetMonitor:
    """Measure event-loop tick gaps to expose work that blocks responsive input."""

    def __init__(
        self,
        *,
        budget_ms: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
        max_recent: int = 20,
        max_samples: int = 5000,
    ) -> None:
        """Create a bounded monitor so instrumentation cannot itself grow unbounded."""
        if max_recent < 0:
            raise ValueError("max_recent must be non-negative")
        if max_samples < 0:
            raise ValueError("max_samples must be non-negative")

        self._budget_ms = float(budget_ms)
        self._clock = clock
        self._max_recent = max_recent
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._recent_stalls: deque[Stall] = deque(maxlen=max_recent)
        self._ticks = 0
        self._stalls = 0
        self._worst_ms = 0.0
        self._previous_tick: float | None = None

    def tick(self) -> Stall | None:
        """Record one loop visit; the first visit establishes a timing baseline."""
        now = self._clock()
        previous = self._previous_tick
        self._previous_tick = now
        self._ticks += 1

        if previous is None:
            return None

        duration_ms = (now - previous) * 1000.0
        self._samples.append(duration_ms)
        self._worst_ms = max(self._worst_ms, duration_ms)

        if duration_ms > self._budget_ms:
            stall = Stall(started_at=previous, duration_ms=duration_ms)
            self._stalls += 1
            self._recent_stalls.append(stall)
            return stall
        return None

    def report(self) -> FrameBudgetReport:
        """Return current evidence without resetting it, enabling periodic HUD updates."""
        if self._ticks < 2 or not self._samples:
            p95_ms = 0.0
        else:
            ordered = sorted(self._samples)
            index = max(0, (95 * len(ordered) + 99) // 100 - 1)
            p95_ms = ordered[index]

        return FrameBudgetReport(
            ticks=self._ticks,
            stalls=self._stalls,
            worst_ms=self._worst_ms,
            p95_ms=p95_ms,
            recent_stalls=tuple(self._recent_stalls),
        )

    def reset(self) -> None:
        """Discard old evidence when a user starts a new performance investigation."""
        self._samples.clear()
        self._recent_stalls.clear()
        self._ticks = 0
        self._stalls = 0
        self._worst_ms = 0.0
        self._previous_tick = None


def install_glib_probe(
    monitor: FrameBudgetMonitor, *, interval_ms: int = 4
) -> int | None:
    """Attach to GLib only when available, preserving headless testability."""
    try:
        from gi.repository import GLib
    except ImportError:
        return None

    def callback() -> bool:
        monitor.tick()
        return True

    return GLib.timeout_add(interval_ms, callback)


def hud_enabled() -> bool:
    """Use an explicit opt-in so diagnostic UI never surprises normal users."""
    return os.environ.get("T1000_PERF_HUD", "").lower() in {"1", "true", "yes"}
