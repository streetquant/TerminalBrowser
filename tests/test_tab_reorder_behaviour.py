from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow


class MasterStub:
    def __init__(self, visible: bool) -> None:
        self.visible = visible
        self.refreshed = 0

    def get_visible(self) -> bool:
        return self.visible

    def refresh(self) -> None:
        self.refreshed += 1


class SideStub:
    def __init__(self) -> None:
        self.refreshed = 0

    def refresh(self) -> None:
        self.refreshed += 1


class TabReorderBehaviourTest(TestCase):
    def test_page_reordered_persists_tab_order_and_refreshes_surfaces(self) -> None:
        calls = []
        side = SideStub()
        master = MasterStub(visible=True)
        window = SimpleNamespace(
            _renumber_visible_tabs=lambda: calls.append('renumber'),
            side=side,
            master_window=master,
        )

        TermDeckWindow._on_page_reordered(window, None, object(), 1)

        self.assertEqual(calls, ['renumber'])
        self.assertEqual(side.refreshed, 1)
        self.assertEqual(master.refreshed, 1)

    def test_page_reordered_skips_hidden_master_refresh(self) -> None:
        calls = []
        side = SideStub()
        master = MasterStub(visible=False)
        window = SimpleNamespace(
            _renumber_visible_tabs=lambda: calls.append('renumber'),
            side=side,
            master_window=master,
        )

        TermDeckWindow._on_page_reordered(window, None, object(), 2)

        self.assertEqual(calls, ['renumber'])
        self.assertEqual(side.refreshed, 1)
        self.assertEqual(master.refreshed, 0)

    def test_page_reordered_handles_absent_master_window(self) -> None:
        calls = []
        side = SideStub()
        window = SimpleNamespace(
            _renumber_visible_tabs=lambda: calls.append('renumber'),
            side=side,
            master_window=None,
        )

        TermDeckWindow._on_page_reordered(window, None, object(), 0)

        self.assertEqual(calls, ['renumber'])
        self.assertEqual(side.refreshed, 1)


if __name__ == '__main__':
    import unittest

    unittest.main()
