from types import MethodType, SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import SidePane


class CheckStub:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def get_active(self) -> bool:
        return self.active

    def set_active(self, active: bool) -> None:
        self.active = active


class SessionCardPointerTest(TestCase):
    def _pane(self):
        menus = []
        pane = SimpleNamespace(selected_ids={"existing"})
        pane._select_record_for_context = MethodType(SidePane._select_record_for_context, pane)
        pane._show_session_menu = lambda record, check, event: menus.append((record.id, check, event))
        return pane, menus

    def test_ctrl_right_click_anywhere_adds_session_to_selection_before_menu(self) -> None:
        pane, menus = self._pane()
        record = SimpleNamespace(id="target")
        check = CheckStub()
        event = SimpleNamespace(
            button=3,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_session_button_press(pane, None, event, record, check)

        self.assertTrue(handled)
        self.assertEqual(pane.selected_ids, {"existing", "target"})
        self.assertTrue(check.get_active())
        self.assertEqual(menus, [("target", check, event)])

    def test_ctrl_right_click_keeps_already_selected_session_selected(self) -> None:
        pane, menus = self._pane()
        pane.selected_ids.add("target")
        record = SimpleNamespace(id="target")
        check = CheckStub(active=True)
        event = SimpleNamespace(
            button=3,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_tag_button_press(pane, None, event, record, check)

        self.assertTrue(handled)
        self.assertEqual(pane.selected_ids, {"existing", "target"})
        self.assertTrue(check.get_active())
        self.assertEqual(menus, [("target", check, event)])


if __name__ == "__main__":
    import unittest

    unittest.main()
