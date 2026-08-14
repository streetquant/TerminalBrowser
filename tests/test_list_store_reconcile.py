"""Behavioural tests for the keyed in-place ListStore reconciler.

The reconciler exists so periodic refreshes never blank a visible table
(clear()+re-append), which is what caused refresh blinking and lost scroll
positions.  A GTK-faithful fake store verifies the diff semantics headlessly:
iters stay valid while their row exists, removal is identity-based, and
sorted stores control their own row order.
"""

from __future__ import annotations

import unittest

from termdeck.gtk_app import _reconcile_list_store


class FakeListStore:
    """Mimic the Gtk.ListStore surface the reconciler touches.

    Iters are the row lists themselves: like GTK's persistent ListStore
    iters they stay valid until their own row is removed, regardless of
    what happens to neighbouring rows.
    """

    def __init__(self, *, sort_active: bool = False) -> None:
        self.rows: list[list[str]] = []
        self.cleared = 0
        self._sort_active = sort_active

    # --- Gtk.TreeModel/TreeSortable surface -------------------------------
    def get_iter_first(self):
        return self.rows[0] if self.rows else None

    def iter_next(self, row):
        for index, candidate in enumerate(self.rows):
            if candidate is row:
                return self.rows[index + 1] if index + 1 < len(self.rows) else None
        raise AssertionError("iter_next called with a stale iter")

    def get_value(self, row, column):
        return row[column]

    def set_value(self, row, column, value):
        row[column] = value

    def insert(self, position, row):
        self.rows.insert(position, list(row))

    def append(self, row):
        self.rows.append(list(row))

    def remove(self, row):
        for index, candidate in enumerate(self.rows):
            if candidate is row:
                del self.rows[index]
                return
        raise AssertionError("remove called with a stale iter")

    def clear(self):
        self.cleared += 1
        self.rows = []

    def get_sort_column_id(self):
        return (self._sort_active, 0, None)

    # --- test helpers ------------------------------------------------------
    def snapshot(self) -> list[list[str]]:
        return [list(row) for row in self.rows]


def seeded(rows: list[list[str]], *, sort_active: bool = False) -> FakeListStore:
    store = FakeListStore(sort_active=sort_active)
    for row in rows:
        store.append(row)
    return store


class ReconcileListStoreTests(unittest.TestCase):
    def test_value_change_updates_in_place_without_clear(self) -> None:
        store = seeded([["a", "1"], ["b", "2"]])
        first_iter = store.rows[0]

        rebuilt = _reconcile_list_store(store, [["a", "9"], ["b", "2"]])

        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.snapshot(), [["a", "9"], ["b", "2"]])
        # The updated row keeps its identity, which is what preserves GTK
        # selection and scroll anchoring across refreshes.
        self.assertIs(store.rows[0], first_iter)

    def test_identical_rows_are_untouched(self) -> None:
        store = seeded([["a", "1"]])
        rebuilt = _reconcile_list_store(store, [["a", "1"]])
        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.snapshot(), [["a", "1"]])

    def test_row_removal_keeps_other_rows_intact(self) -> None:
        store = seeded([["a", "1"], ["b", "2"], ["c", "3"]])
        survivor = store.rows[2]

        rebuilt = _reconcile_list_store(store, [["a", "1"], ["c", "3"]])

        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.snapshot(), [["a", "1"], ["c", "3"]])
        self.assertIs(store.rows[1], survivor)

    def test_new_row_inserted_at_its_position(self) -> None:
        store = seeded([["a", "1"], ["c", "3"]])

        rebuilt = _reconcile_list_store(store, [["a", "1"], ["b", "2"], ["c", "3"]])

        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.snapshot(), [["a", "1"], ["b", "2"], ["c", "3"]])

    def test_reorder_falls_back_to_rebuild(self) -> None:
        store = seeded([["a", "1"], ["b", "2"]])

        rebuilt = _reconcile_list_store(store, [["b", "2"], ["a", "1"]])

        self.assertTrue(rebuilt)
        self.assertEqual(store.cleared, 1)
        self.assertEqual(store.snapshot(), [["b", "2"], ["a", "1"]])

    def test_duplicate_keys_fall_back_to_rebuild(self) -> None:
        store = seeded([["a", "1"]])

        rebuilt = _reconcile_list_store(store, [["a", "1"], ["a", "2"]])

        self.assertTrue(rebuilt)
        self.assertEqual(store.snapshot(), [["a", "1"], ["a", "2"]])

    def test_duplicate_existing_keys_fall_back_to_rebuild(self) -> None:
        store = seeded([["a", "1"], ["a", "2"]])

        rebuilt = _reconcile_list_store(store, [["a", "1"]])

        self.assertTrue(rebuilt)
        self.assertEqual(store.snapshot(), [["a", "1"]])

    def test_sorted_store_appends_and_never_rebuilds_on_order(self) -> None:
        # With user-activated column sorting the model owns row order, so an
        # order mismatch must not trigger the rebuild fallback (that would
        # reintroduce the blink for sorted views on every tick).
        store = seeded([["b", "2"], ["a", "1"]], sort_active=True)

        rebuilt = _reconcile_list_store(store, [["a", "1"], ["b", "2"], ["c", "3"]])

        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        # The fake does not re-sort; production GTK placement is driven by the
        # model's sort function.  What matters is content and no rebuild.
        self.assertEqual(
            sorted(store.snapshot()), [["a", "1"], ["b", "2"], ["c", "3"]]
        )

    def test_empty_desired_rows_empties_the_store_in_place(self) -> None:
        store = seeded([["a", "1"], ["b", "2"]])

        rebuilt = _reconcile_list_store(store, [])

        self.assertFalse(rebuilt)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
