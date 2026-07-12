import unittest

from termdeck.capacity import bounded_registry_records, bounded_startup_groups
from termdeck.models import SessionRecord


def _record(index: int, *, window: int = 0, pinned: bool = False) -> SessionRecord:
    stamp = f"2026-07-11T12:{index // 60:02d}:{index % 60:02d}+00:00"
    return SessionRecord(
        id=f"td-{window}-{index}",
        name=f"agent-{index}",
        tag="ai",
        command="codex",
        cwd="/tmp",
        tmux_name=f"termdeck_ai_{window}_{index}",
        created_at=stamp,
        last_opened_at=stamp,
        status="running",
        last_pid=10_000 + index,
        window_id=f"window-{window}",
        window_title=f"Window {window}",
        tab_index=index,
        visible=True,
        pinned=pinned,
    )


class CapacityTests(unittest.TestCase):
    def test_two_thousand_session_restore_materialises_bounded_working_set(self) -> None:
        groups = []
        pinned_id = ""
        for window in range(10):
            records = []
            for index in range(200):
                pinned = window == 0 and index == 0
                record = _record(index, window=window, pinned=pinned)
                if pinned:
                    pinned_id = record.id
                records.append(record)
            groups.append((f"window-{window}", f"Window {window}", records))

        selected = bounded_startup_groups(groups)

        restored = [record for _window_id, _title, records in selected for record in records]
        self.assertLessEqual(len(selected), 4)
        self.assertLessEqual(len(restored), 24)
        self.assertTrue(all(len(records) <= 12 for _id, _title, records in selected))
        self.assertIn(pinned_id, {record.id for record in restored})
        self.assertEqual(sum(len(records) for _id, _title, records in groups), 2000)

    def test_registry_view_keeps_selected_and_pinned_rows_without_reordering(self) -> None:
        records = [_record(index, pinned=index == 190) for index in range(200)]
        selected = bounded_registry_records(records, limit=10, preferred_ids={records[3].id})

        self.assertEqual(len(selected), 10)
        self.assertIn(records[3], selected)
        self.assertIn(records[190], selected)
        original_positions = [records.index(record) for record in selected]
        self.assertEqual(original_positions, sorted(original_positions))


if __name__ == "__main__":
    unittest.main()
