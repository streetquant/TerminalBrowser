from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow


class ChildStub:
    def __init__(self, name: str) -> None:
        self.name = name
        self.parent = None

    def get_parent(self):
        return self.parent

    def __repr__(self) -> str:
        return self.name


class PanedStub:
    def __init__(self) -> None:
        self.calls = []

    def remove(self, child) -> None:
        self.calls.append(('remove', child.name))
        child.parent = None

    def pack1(self, child, resize: bool, shrink: bool) -> None:
        self.calls.append(('pack1', child.name, resize, shrink))
        child.parent = self

    def pack2(self, child, resize: bool, shrink: bool) -> None:
        self.calls.append(('pack2', child.name, resize, shrink))
        child.parent = self

    def set_position(self, position: int) -> None:
        self.calls.append(('position', position))

    def show_all(self) -> None:
        self.calls.append(('show_all',))


def make_pack_window(position: str):
    paned = PanedStub()
    notebook = ChildStub('notebook')
    side = ChildStub('side')
    notebook.parent = paned
    side.parent = paned
    return SimpleNamespace(paned=paned, notebook=notebook, side=side, side_position=position)


class SidePanePositionBehaviourTest(TestCase):
    def test_pack_side_pane_left_places_sessions_before_notebook(self) -> None:
        window = make_pack_window('left')

        TermDeckWindow._pack_side_pane(window)

        self.assertEqual(
            window.paned.calls,
            [
                ('remove', 'notebook'),
                ('remove', 'side'),
                ('pack1', 'side', False, True),
                ('pack2', 'notebook', True, True),
                ('position', 340),
                ('show_all',),
            ],
        )

    def test_pack_side_pane_right_places_notebook_before_sessions(self) -> None:
        window = make_pack_window('right')

        TermDeckWindow._pack_side_pane(window)

        self.assertEqual(
            window.paned.calls,
            [
                ('remove', 'notebook'),
                ('remove', 'side'),
                ('pack1', 'notebook', True, True),
                ('pack2', 'side', False, True),
                ('position', 980),
                ('show_all',),
            ],
        )

    def test_set_side_pane_position_persists_repacks_and_toasts(self) -> None:
        calls = []
        window = SimpleNamespace(
            side_position='right',
            _pack_side_pane=lambda: calls.append(('pack',)),
            app_context=SimpleNamespace(apply_side_pane_position=lambda position: calls.append(('persist', position))),
            show_toast=lambda text, **kwargs: calls.append(('toast', text, kwargs)),
        )

        TermDeckWindow.set_side_pane_position(window, 'LEFT')

        self.assertEqual(window.side_position, 'left')
        self.assertEqual(
            calls,
            [
                ('pack',),
                ('persist', 'left'),
                ('toast', 'Tabs pane moved to left', {'kind': 'info'}),
            ],
        )

    def test_set_side_pane_position_defaults_unknown_values_to_right_without_persist_when_disabled(self) -> None:
        calls = []
        window = SimpleNamespace(
            side_position='left',
            _pack_side_pane=lambda: calls.append(('pack',)),
            app_context=SimpleNamespace(apply_side_pane_position=lambda position: calls.append(('persist', position))),
            show_toast=lambda text, **kwargs: calls.append(('toast', text, kwargs)),
        )

        TermDeckWindow.set_side_pane_position(window, 'elsewhere', persist=False)

        self.assertEqual(window.side_position, 'right')
        self.assertEqual(
            calls,
            [
                ('pack',),
                ('toast', 'Tabs pane moved to right', {'kind': 'info'}),
            ],
        )


if __name__ == '__main__':
    import unittest

    unittest.main()
