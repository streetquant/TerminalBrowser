"""Shared, read-only runtime assertions for TermDeck smoke programs.

The GUI intentionally exposes no public testing API.  Keep the small amount of
private state observation needed by end-to-end smokes in one place so transport
architecture changes cannot leave a collection of contradictory readiness
oracles behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamSnapshot:
    """One lock-consistent view of an AttachTerminal stream controller."""

    controller: Any
    ready: bool
    stopped: bool
    generation: int
    size: tuple[int, int]
    state: Any


def stream_snapshot(widget: Any) -> StreamSnapshot | None:
    """Return a lock-consistent controller snapshot, or ``None`` before attach."""

    controller = getattr(widget, "controller", None)
    if controller is None:
        return None
    lock = getattr(controller, "_lock", None)

    def capture() -> StreamSnapshot:
        columns, rows = getattr(controller, "_size", (0, 0))
        return StreamSnapshot(
            controller=controller,
            ready=bool(getattr(controller, "_ready", False)),
            stopped=bool(getattr(controller, "_stopped", False)),
            generation=int(getattr(widget, "_stream_generation", 0)),
            size=(int(columns), int(rows)),
            state=getattr(controller, "state", None),
        )

    if lock is None:
        return capture()
    with lock:
        return capture()


def stream_ready(widget: Any) -> bool:
    """True only after the current controller committed setup and is live."""

    snapshot = stream_snapshot(widget)
    return snapshot is not None and snapshot.ready and not snapshot.stopped
