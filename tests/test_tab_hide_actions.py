from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow


def make_record(session_id: str, tab_index: int, *, pinned: bool = False):
    return SimpleNamespace(id=session_id, tab_index=tab_index, pinned=pinned)


def make_window(records):
    hidden = []
    toasts = []
    window = SimpleNamespace(
        tabs={record.id: SimpleNamespace(record=record) for record in records},
        show_toast=lambda text, **kwargs: toasts.append((text, kwargs)),
    )

    def hide_tab(session_id: str, *, include_pinned: bool = False) -> None:
        tab = window.tabs.get(session_id)
        if tab is None:
            return
        record = tab.record
        if record.pinned and not include_pinned:
            return
        hidden.append(session_id)
        window.tabs.pop(session_id, None)

    window.hide_tab = hide_tab
    window._visible_tab_records = lambda: TermDeckWindow._visible_tab_records(window)
    window._hide_tab_records = lambda records: TermDeckWindow._hide_tab_records(window, records)
    return window, hidden, toasts


class TabHideActionsTest(TestCase):
    def test_visible_tab_records_are_sorted_by_tab_index(self) -> None:
        records = [
            make_record("third", 2),
            make_record("first", 0),
            make_record("second", 1),
        ]
        window, _hidden, _toasts = make_window(records)

        ordered = TermDeckWindow._visible_tab_records(window)

        self.assertEqual([record.id for record in ordered], ["first", "second", "third"])

    def test_hide_other_tabs_keeps_target_and_respects_pinned_guard(self) -> None:
        target = make_record("target", 1)
        left = make_record("left", 0)
        pinned = make_record("pinned", 2, pinned=True)
        right = make_record("right", 3)
        window, hidden, toasts = make_window([target, left, pinned, right])

        TermDeckWindow._hide_other_tabs(window, target)

        self.assertEqual(hidden, ["left", "right"])
        self.assertEqual(sorted(window.tabs), ["pinned", "target"])
        self.assertEqual(toasts, [("Hid 2 tab(s)", {"kind": "info"})])

    def test_hide_tabs_to_right_uses_tab_index_order_and_keeps_left(self) -> None:
        left = make_record("left", 0)
        target = make_record("target", 1)
        right = make_record("right", 2)
        far_right = make_record("far-right", 3)
        window, hidden, toasts = make_window([far_right, target, left, right])

        TermDeckWindow._hide_tabs_to_right(window, target)

        self.assertEqual(hidden, ["right", "far-right"])
        self.assertEqual(sorted(window.tabs), ["left", "target"])
        self.assertEqual(toasts, [("Hid 2 tab(s)", {"kind": "info"})])

    def test_hide_tabs_to_right_skips_pinned_tabs_without_false_toast_count(self) -> None:
        target = make_record("target", 0)
        pinned = make_record("pinned", 1, pinned=True)
        right = make_record("right", 2)
        window, hidden, toasts = make_window([target, pinned, right])

        TermDeckWindow._hide_tabs_to_right(window, target)

        self.assertEqual(hidden, ["right"])
        self.assertEqual(sorted(window.tabs), ["pinned", "target"])
        self.assertEqual(toasts, [("Hid 1 tab(s)", {"kind": "info"})])

    def test_hide_tab_records_does_not_toast_when_pinned_guard_hides_nothing(self) -> None:
        pinned = make_record("pinned", 0, pinned=True)
        window, hidden, toasts = make_window([pinned])

        TermDeckWindow._hide_tab_records(window, [pinned])

        self.assertEqual(hidden, [])
        self.assertEqual(sorted(window.tabs), ["pinned"])
        self.assertEqual(toasts, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
