from __future__ import annotations

import unittest

from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.task_snapshot import build_task_snapshot
from termdeck.window_snapshot import build_window_snapshots, filter_window_snapshots, render_window_table, sort_window_snapshots, window_snapshots_to_dicts


class FakeBackend:
    available = False


class FakeSampler:
    def __init__(self, snapshots: dict[int | None, ResourceSnapshot]):
        self.snapshots = snapshots

    def snapshot(self, pid: int | None) -> ResourceSnapshot:
        return self.snapshots.get(pid, ResourceSnapshot(pid=pid, exists=False))


def record(session_id: str, name: str, window: str, *, pid: int | None, tab_index: int, status: str = "running") -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=name,
        tag="dev" if window == "Dev" else "ai",
        command=f"echo {name}",
        cwd="/work/app",
        tmux_name=f"termdeck_{session_id}",
        created_at="2026-07-08T00:00:00+00:00",
        last_opened_at="2026-07-08T00:00:00+00:00",
        status=status,
        last_pid=pid,
        window_id=f"win-{window.casefold()}",
        window_title=window,
        tab_index=tab_index,
        visible=True,
    )


class WindowSnapshotTests(unittest.TestCase):
    def test_window_snapshot_aggregates_tab_resources_and_health(self) -> None:
        records = [
            record("td-api", "api", "Dev", pid=101, tab_index=0),
            record("td-tests", "tests", "Dev", pid=202, tab_index=1),
            record("td-agent", "codex", "Agents", pid=303, tab_index=0),
        ]
        sampler = FakeSampler(
            {
                101: ResourceSnapshot(pid=101, exists=True, state="S", cpu_percent=25.0, memory_bytes=100, runtime_seconds=10, process_count=2),
                202: ResourceSnapshot(pid=202, exists=True, state="S", cpu_percent=350.0, memory_bytes=200, runtime_seconds=20, process_count=3),
                303: ResourceSnapshot(pid=303, exists=True, state="T", cpu_percent=0.0, memory_bytes=50, runtime_seconds=5, process_count=1),
            }
        )
        items = build_task_snapshot(records, backend=FakeBackend(), sampler=sampler)
        rows = build_window_snapshots(items)
        payload = {row["title"]: row for row in window_snapshots_to_dicts(rows)}
        self.assertEqual(payload["Dev"]["tab_count"], 2)
        self.assertEqual(payload["Dev"]["running_count"], 2)
        self.assertEqual(payload["Dev"]["cpu_percent"], 375.0)
        self.assertEqual(payload["Dev"]["memory_bytes"], 300)
        self.assertEqual(payload["Dev"]["critical_count"], 1)
        self.assertEqual(payload["Agents"]["paused_count"], 1)

    def test_window_snapshot_filter_sort_and_table(self) -> None:
        records = [
            record("td-api", "api", "Dev", pid=101, tab_index=0),
            record("td-agent", "codex", "Agents", pid=202, tab_index=0),
        ]
        sampler = FakeSampler(
            {
                101: ResourceSnapshot(pid=101, exists=True, state="S", cpu_percent=5.0, memory_bytes=10, runtime_seconds=1, process_count=1),
                202: ResourceSnapshot(pid=202, exists=True, state="S", cpu_percent=200.0, memory_bytes=20, runtime_seconds=2, process_count=1),
            }
        )
        rows = build_window_snapshots(build_task_snapshot(records, backend=FakeBackend(), sampler=sampler))
        self.assertEqual([row.title for row in filter_window_snapshots(rows, "agents high")], ["Agents"])
        self.assertEqual([row.title for row in sort_window_snapshots(rows, "cpu")], ["Agents", "Dev"])
        table = render_window_table(rows)
        self.assertIn("Window", table)
        self.assertIn("Agents", table)
        self.assertIn("Top health", table)


if __name__ == "__main__":
    unittest.main()
