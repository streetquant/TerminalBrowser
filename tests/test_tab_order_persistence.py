from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow


class NotebookStub:
    def __init__(self, pages):
        self.pages = list(pages)

    def get_n_pages(self) -> int:
        return len(self.pages)

    def get_nth_page(self, index: int):
        return self.pages[index]


class StoreStub:
    def __init__(self, *, missing_ids=None) -> None:
        self.missing_ids = set(missing_ids or [])
        self.updates = []

    def update(self, session_id: str, **changes):
        if session_id in self.missing_ids:
            raise KeyError(session_id)
        self.updates.append((session_id, changes))
        return SimpleNamespace(id=session_id, **changes)


class TabOrderPersistenceTest(TestCase):
    def test_renumber_visible_tabs_persists_notebook_order(self) -> None:
        first = object()
        second = object()
        third = object()
        store = StoreStub()
        window = SimpleNamespace(
            notebook=NotebookStub([third, first, second]),
            tabs={
                "first": SimpleNamespace(widget=first),
                "second": SimpleNamespace(widget=second),
                "third": SimpleNamespace(widget=third),
            },
            app_context=SimpleNamespace(store=store),
            window_id="win-1",
            window_title="Main",
        )

        TermDeckWindow._renumber_visible_tabs(window)

        self.assertEqual(
            store.updates,
            [
                ("third", {"tab_index": 0, "window_id": "win-1", "window_title": "Main"}),
                ("first", {"tab_index": 1, "window_id": "win-1", "window_title": "Main"}),
                ("second", {"tab_index": 2, "window_id": "win-1", "window_title": "Main"}),
            ],
        )

    def test_renumber_visible_tabs_ignores_unknown_notebook_pages(self) -> None:
        known = object()
        unknown = object()
        store = StoreStub()
        window = SimpleNamespace(
            notebook=NotebookStub([unknown, known]),
            tabs={"known": SimpleNamespace(widget=known)},
            app_context=SimpleNamespace(store=store),
            window_id="win-2",
            window_title="Secondary",
        )

        TermDeckWindow._renumber_visible_tabs(window)

        self.assertEqual(
            store.updates,
            [("known", {"tab_index": 1, "window_id": "win-2", "window_title": "Secondary"})],
        )

    def test_renumber_visible_tabs_continues_when_store_record_is_missing(self) -> None:
        missing = object()
        present = object()
        store = StoreStub(missing_ids={"missing"})
        window = SimpleNamespace(
            notebook=NotebookStub([missing, present]),
            tabs={
                "missing": SimpleNamespace(widget=missing),
                "present": SimpleNamespace(widget=present),
            },
            app_context=SimpleNamespace(store=store),
            window_id="win-3",
            window_title="Recovered",
        )

        TermDeckWindow._renumber_visible_tabs(window)

        self.assertEqual(
            store.updates,
            [("present", {"tab_index": 1, "window_id": "win-3", "window_title": "Recovered"})],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
