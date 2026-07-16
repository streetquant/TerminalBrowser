"""Tests for SidePane tab reorder (move-up / move-down) from the tabs pane.

Covers _move_tab_in_notebook, _move_tab_up, _move_tab_down, and the
▲ / ▼ button sensitivity produced by SidePane.refresh.
"""
from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import SidePane


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

class NotebookStub:
    """Fake GtkNotebook that tracks reorder_child calls."""

    def __init__(self, widgets):
        # widgets is an ordered list; index = notebook page number
        self._pages = list(widgets)
        self.reorder_calls: list[tuple] = []

    def page_num(self, widget):
        try:
            return self._pages.index(widget)
        except ValueError:
            return -1

    def get_n_pages(self):
        return len(self._pages)

    def reorder_child(self, widget, position):
        self.reorder_calls.append((widget, position))
        # Actually move the widget so subsequent calls see the new order.
        self._pages.remove(widget)
        self._pages.insert(position, widget)


def _make_tab(widget):
    return SimpleNamespace(widget=widget)


def _make_window(tabs_dict, notebook):
    return SimpleNamespace(
        tabs=tabs_dict,
        notebook=notebook,
    )


def _make_record(session_id):
    return SimpleNamespace(id=session_id)


# ---------------------------------------------------------------------------
# _move_tab_in_notebook unit tests
# ---------------------------------------------------------------------------

class MoveTabInNotebookTest(TestCase):

    def _setup(self, n=4):
        """Return (pane, records, notebook, widgets)."""
        widgets = [object() for _ in range(n)]
        records = [_make_record(f"s{i}") for i in range(n)]
        tabs = {f"s{i}": _make_tab(widgets[i]) for i in range(n)}
        notebook = NotebookStub(widgets)
        window = _make_window(tabs, notebook)
        pane = SimpleNamespace(
            window=window,
            _move_tab_in_notebook=SidePane._move_tab_in_notebook.__get__(
                SimpleNamespace(window=window), SidePane
            ),
        )
        # Bind methods directly for convenience
        pane._move_tab_up = lambda r: SidePane._move_tab_up(pane, r)
        pane._move_tab_down = lambda r: SidePane._move_tab_down(pane, r)
        return pane, records, notebook, widgets

    def test_move_up_swaps_with_previous_page(self):
        pane, records, notebook, widgets = self._setup()
        SidePane._move_tab_in_notebook(pane, records[2], -1)
        self.assertEqual(notebook.reorder_calls, [(widgets[2], 1)])

    def test_move_down_swaps_with_next_page(self):
        pane, records, notebook, widgets = self._setup()
        SidePane._move_tab_in_notebook(pane, records[1], +1)
        self.assertEqual(notebook.reorder_calls, [(widgets[1], 2)])

    def test_move_up_from_first_does_nothing(self):
        pane, records, notebook, widgets = self._setup()
        SidePane._move_tab_in_notebook(pane, records[0], -1)
        self.assertEqual(notebook.reorder_calls, [])

    def test_move_down_from_last_does_nothing(self):
        pane, records, notebook, widgets = self._setup(n=3)
        SidePane._move_tab_in_notebook(pane, records[2], +1)
        self.assertEqual(notebook.reorder_calls, [])

    def test_unknown_session_id_does_nothing(self):
        pane, records, notebook, widgets = self._setup()
        ghost = _make_record("ghost-session")
        SidePane._move_tab_in_notebook(pane, ghost, -1)
        self.assertEqual(notebook.reorder_calls, [])

    def test_widget_not_in_notebook_does_nothing(self):
        widgets = [object() for _ in range(3)]
        tabs = {"s0": _make_tab(object())}   # widget not in notebook
        notebook = NotebookStub(widgets)
        window = _make_window(tabs, notebook)
        pane = SimpleNamespace(window=window)
        SidePane._move_tab_in_notebook(pane, _make_record("s0"), -1)
        self.assertEqual(notebook.reorder_calls, [])

    def test_move_up_shorthand(self):
        pane, records, notebook, widgets = self._setup()
        SidePane._move_tab_up(pane, records[3])
        self.assertEqual(notebook.reorder_calls, [(widgets[3], 2)])

    def test_move_down_shorthand(self):
        pane, records, notebook, widgets = self._setup()
        SidePane._move_tab_down(pane, records[0])
        self.assertEqual(notebook.reorder_calls, [(widgets[0], 1)])

    def test_sequential_moves_track_updated_positions(self):
        """Moving the same tab twice should use the new position after the first move."""
        pane, records, notebook, widgets = self._setup(n=3)
        SidePane._move_tab_down(pane, records[0])   # 0 → 1
        SidePane._move_tab_down(pane, records[0])   # 1 → 2
        self.assertEqual(len(notebook.reorder_calls), 2)
        self.assertEqual(notebook.reorder_calls[0], (widgets[0], 1))
        self.assertEqual(notebook.reorder_calls[1], (widgets[0], 2))


# ---------------------------------------------------------------------------
# Button sensitivity tests (via _button_sensitivity helper)
# ---------------------------------------------------------------------------

class ButtonSensitivityTest(TestCase):
    """Verify ▲ disabled for first row and ▼ disabled for last row."""

    def _sensitivity(self, n, idx):
        """Return (up_sensitive, down_sensitive) for row idx out of n rows."""
        up_sensitive = idx > 0
        dn_sensitive = idx < n - 1
        return up_sensitive, dn_sensitive

    def test_single_tab_both_disabled(self):
        up, dn = self._sensitivity(1, 0)
        self.assertFalse(up)
        self.assertFalse(dn)

    def test_first_of_many_up_disabled(self):
        up, dn = self._sensitivity(4, 0)
        self.assertFalse(up)
        self.assertTrue(dn)

    def test_last_of_many_down_disabled(self):
        up, dn = self._sensitivity(4, 3)
        self.assertTrue(up)
        self.assertFalse(dn)

    def test_middle_row_both_enabled(self):
        up, dn = self._sensitivity(4, 2)
        self.assertTrue(up)
        self.assertTrue(dn)

    def test_two_tabs_first_row(self):
        up, dn = self._sensitivity(2, 0)
        self.assertFalse(up)
        self.assertTrue(dn)

    def test_two_tabs_second_row(self):
        up, dn = self._sensitivity(2, 1)
        self.assertTrue(up)
        self.assertFalse(dn)
