from types import MethodType, SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow


class ChildStub:
    def __init__(self, name: str, preferred_width=(0, 0)) -> None:
        self.name = name
        self.parent = None
        self.preferred_width = preferred_width
        self.calls: list[tuple[object, ...]] = []
        self.child_visible = True
        self.visible = True
        self.no_show_all = False

    def get_parent(self):
        return self.parent

    def get_preferred_width(self):
        return self.preferred_width

    def set_no_show_all(self, value: bool) -> None:
        self.no_show_all = bool(value)
        self.calls.append(("no-show-all", bool(value)))

    def set_child_visible(self, value: bool) -> None:
        self.child_visible = bool(value)
        self.calls.append(("child-visible", bool(value)))

    def show(self) -> None:
        self.visible = True
        self.calls.append(("show",))

    def hide(self) -> None:
        self.visible = False
        self.calls.append(("hide",))

    def set_visible(self, value: bool) -> None:
        self.visible = bool(value)
        self.calls.append(("visible", bool(value)))

    def __repr__(self) -> str:
        return self.name


class PanedStub:
    def __init__(self, width: int = 1320) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.width = width
        self.children: list[ChildStub] = []
        self.position = 0

    def remove(self, child) -> None:
        self.calls.append(("remove", child.name))
        if child in self.children:
            self.children.remove(child)
        child.parent = None

    def pack1(self, child, resize: bool, shrink: bool) -> None:
        self.calls.append(("pack1", child.name, resize, shrink))
        self.children.insert(0, child)
        child.parent = self

    def pack2(self, child, resize: bool, shrink: bool) -> None:
        self.calls.append(("pack2", child.name, resize, shrink))
        self.children.append(child)
        child.parent = self

    def set_position(self, position: int) -> None:
        self.position = int(position)
        self.calls.append(("position", int(position)))

    def get_allocated_width(self) -> int:
        return self.width

    def show(self) -> None:
        self.calls.append(("show",))


def make_pack_window(position: str, *, visible: bool = True):
    paned = PanedStub()
    notebook = ChildStub("notebook")
    side = ChildStub("side")
    notebook.parent = paned
    side.parent = paned
    paned.children = [notebook, side]
    geometry: list[str] = []
    window = SimpleNamespace(
        paned=paned,
        notebook=notebook,
        side=side,
        side_position=position,
        tabs_pane_visible=visible,
        _queue_current_terminal_geometry_sync=lambda: geometry.append("sync"),
    )
    window._apply_side_pane_visibility = MethodType(
        TermDeckWindow._apply_side_pane_visibility,
        window,
    )
    return window, geometry


class SidePanePositionBehaviourTest(TestCase):
    def test_pack_side_pane_left_places_sessions_before_notebook(self) -> None:
        window, geometry = make_pack_window("left")

        TermDeckWindow._pack_side_pane(window)

        self.assertEqual(window.paned.children, [window.side, window.notebook])
        self.assertEqual(window.paned.position, 340)
        self.assertIs(window.notebook.parent, window.paned)
        self.assertIs(window.side.parent, window.paned)
        self.assertTrue(window.side.child_visible)
        self.assertTrue(window.side.visible)
        self.assertEqual(geometry, ["sync"])

    def test_pack_side_pane_right_places_notebook_before_sessions(self) -> None:
        window, _geometry = make_pack_window("right")

        TermDeckWindow._pack_side_pane(window)

        self.assertEqual(window.paned.children, [window.notebook, window.side])
        self.assertEqual(window.paned.position, 980)
        self.assertIs(window.notebook.parent, window.paned)
        self.assertIs(window.side.parent, window.paned)

    def test_pack_side_pane_expands_for_scaled_sidebar_minimum(self) -> None:
        window, _geometry = make_pack_window("left")
        window.side.preferred_width = (444, 574)

        TermDeckWindow._pack_side_pane(window)

        self.assertEqual(window.paned.position, 444)

    def test_hidden_sidebar_collapses_without_unparenting_notebook_or_side(self) -> None:
        window, geometry = make_pack_window("left", visible=False)
        notebook_parent = window.notebook.parent
        side_parent = window.side.parent

        TermDeckWindow._apply_side_pane_visibility(window)

        self.assertIs(window.notebook.parent, notebook_parent)
        self.assertIs(window.side.parent, side_parent)
        self.assertEqual(window.paned.position, 0)
        self.assertFalse(window.side.child_visible)
        self.assertFalse(window.side.visible)
        self.assertEqual(geometry, ["sync"])
        self.assertFalse(any(call[0] == "remove" for call in window.paned.calls))

    def test_visibility_toggle_never_repacks_or_remaps_live_terminal_children(self) -> None:
        window, geometry = make_pack_window("right", visible=False)
        events: list[object] = []
        window.app_context = SimpleNamespace(
            apply_tabs_pane_visibility=lambda visible: events.append(("persist", visible))
        )
        window.show_toast = lambda text, **kwargs: events.append(("toast", text, kwargs))
        window._queue_side_pane_layout_repack = lambda: events.append("settle")
        window.side.refresh = lambda: events.append("refresh")
        window._pack_side_pane = lambda: events.append("REPACKED")
        notebook_parent = window.notebook.parent
        side_parent = window.side.parent

        TermDeckWindow.set_tabs_pane_visible(window, True)
        TermDeckWindow.set_tabs_pane_visible(window, False, persist=False, notify=False)

        self.assertNotIn("REPACKED", events)
        self.assertIs(window.notebook.parent, notebook_parent)
        self.assertIs(window.side.parent, side_parent)
        self.assertFalse(any(call[0] == "remove" for call in window.paned.calls))
        self.assertGreaterEqual(geometry.count("sync"), 2)

    def test_set_side_pane_position_persists_repacks_and_toasts(self) -> None:
        calls = []
        window = SimpleNamespace(
            side_position="right",
            tabs_pane_visible=True,
            _pack_side_pane=lambda: calls.append(("pack",)),
            _queue_side_pane_layout_repack=lambda: calls.append(("settle",)),
            app_context=SimpleNamespace(
                apply_side_pane_position=lambda position: calls.append(("persist", position))
            ),
            show_toast=lambda text, **kwargs: calls.append(("toast", text, kwargs)),
        )

        TermDeckWindow.set_side_pane_position(window, "LEFT")

        self.assertEqual(window.side_position, "left")
        self.assertEqual(
            calls,
            [
                ("pack",),
                ("settle",),
                ("persist", "left"),
                ("toast", "Tabs pane moved to left", {"kind": "info"}),
            ],
        )

    def test_set_side_pane_position_defaults_unknown_values_to_right_without_persist_when_disabled(self) -> None:
        calls = []
        window = SimpleNamespace(
            side_position="left",
            tabs_pane_visible=False,
            _pack_side_pane=lambda: calls.append(("pack",)),
            _queue_side_pane_layout_repack=lambda: calls.append(("settle",)),
            app_context=SimpleNamespace(
                apply_side_pane_position=lambda position: calls.append(("persist", position))
            ),
            show_toast=lambda text, **kwargs: calls.append(("toast", text, kwargs)),
        )

        TermDeckWindow.set_side_pane_position(window, "elsewhere", persist=False)

        self.assertEqual(window.side_position, "right")
        self.assertEqual(
            calls,
            [
                ("pack",),
                ("toast", "Tabs pane moved to right", {"kind": "info"}),
            ],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
