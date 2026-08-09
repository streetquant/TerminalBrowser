from __future__ import annotations

from .models import ResourceSnapshot


def merge_confirmed_pane_snapshot(
    snapshot: ResourceSnapshot,
    *,
    pane_pid: int | None,
    pane_confirmed: bool,
) -> ResourceSnapshot:
    """Preserve fresh tmux liveness when this process cannot inspect the PID.

    A TermDeck GUI can share a tmux socket with a host process while running in
    a different PID namespace. In that case ``/proc/<pane_pid>`` is absent even
    though tmux has just reported a non-dead pane. Keep resource values unknown
    until they become observable; never promote remembered or dead pane PIDs.
    """

    if snapshot.exists or not pane_confirmed or pane_pid is None:
        return snapshot
    return ResourceSnapshot(pid=pane_pid, exists=True, state="unknown")
