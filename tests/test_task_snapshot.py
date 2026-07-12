from __future__ import annotations

import os
import unittest

from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.task_snapshot import build_task_snapshot, filter_task_snapshot, render_task_table, sort_task_snapshot, task_snapshot_to_dicts


class FakeBackend:
    available = False


class StaleBackend:
    available = True
    pane_snapshot_stale = True

    def pane_pids(self, records):
        return {row.id: row.last_pid for row in records}


class FakeSampler:
    def __init__(self, snapshots: dict[int | None, ResourceSnapshot]):
        self.snapshots = snapshots

    def snapshot(self, pid: int | None) -> ResourceSnapshot:
        return self.snapshots.get(pid, ResourceSnapshot(pid=pid, exists=False))


class FailingSampler:
    def snapshot(self, _pid):
        raise AssertionError("stale backend data must not be sampled as authoritative")


def record(session_id: str, name: str, *, pid: int | None, tab_index: int = 0, tag: str = "dev") -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=name,
        tag=tag,
        command=f"echo {name}",
        cwd="/work/app",
        tmux_name=f"termdeck_{session_id}",
        created_at="2026-07-08T00:00:00+00:00",
        last_opened_at="2026-07-08T00:00:00+00:00",
        status="running",
        last_pid=pid,
        window_id="win-1",
        window_title="Dev",
        tab_index=tab_index,
        visible=True,
    )


class TaskSnapshotTests(unittest.TestCase):
    def test_stale_backend_snapshot_renders_sampling_instead_of_dead(self) -> None:
        row = record("td-stale", "codex", pid=101)

        items = build_task_snapshot([row], backend=StaleBackend(), sampler=FailingSampler())

        self.assertEqual(items[0].health.label, "sampling")
        self.assertEqual(items[0].row.state, "running")

    def test_build_snapshot_uses_remembered_pid_when_tmux_is_unavailable(self) -> None:
        pid = os.getpid()
        items = build_task_snapshot(
            [record("td-one", "api", pid=pid)],
            backend=FakeBackend(),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].pid, pid)
        self.assertEqual(items[0].row.focus_key, "Alt+1")
        self.assertEqual(items[0].row.pause_key, "Ctrl+Alt+1")
        payload = task_snapshot_to_dicts(items)
        self.assertEqual(payload[0]["id"], "td-one")
        self.assertIn("health", payload[0])

    def test_filter_sort_and_table_rendering(self) -> None:
        records = [
            record("td-api", "api", pid=101, tab_index=0, tag="dev"),
            record("td-agent", "codex", pid=202, tab_index=1, tag="ai"),
        ]
        sampler = FakeSampler(
            {
                101: ResourceSnapshot(pid=101, exists=True, state="S", cpu_percent=5.0, memory_bytes=10, runtime_seconds=1, process_count=1),
                202: ResourceSnapshot(pid=202, exists=True, state="S", cpu_percent=250.0, memory_bytes=20, runtime_seconds=2, process_count=1),
            }
        )
        items = build_task_snapshot(records, backend=FakeBackend(), sampler=sampler)
        self.assertEqual([item.record.name for item in sort_task_snapshot(items, "cpu")], ["codex", "api"])
        self.assertEqual([item.record.name for item in filter_task_snapshot(items, "ai codex")], ["codex"])
        table = render_task_table(items)
        self.assertIn("Ctrl+Alt+2", table)
        self.assertIn("codex", table)


if __name__ == "__main__":
    unittest.main()
