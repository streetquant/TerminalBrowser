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
        pane._toggle_record_selection = MethodType(SidePane._toggle_record_selection, pane)
        pane._show_session_menu = lambda record, check, event: menus.append((record.id, check, event))
        return pane, menus

    def test_ctrl_left_click_anywhere_toggles_session_into_selection(self) -> None:
        pane, menus = self._pane()
        record = SimpleNamespace(id="target")
        check = CheckStub()
        event = SimpleNamespace(
            button=1,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_session_button_press(pane, None, event, record, check)

        self.assertTrue(handled)
        self.assertEqual(pane.selected_ids, {"existing", "target"})
        self.assertTrue(check.get_active())
        self.assertEqual(menus, [])

    def test_ctrl_left_click_anywhere_toggles_selected_session_off(self) -> None:
        pane, menus = self._pane()
        pane.selected_ids.add("target")
        record = SimpleNamespace(id="target")
        check = CheckStub(active=True)
        event = SimpleNamespace(
            button=1,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_session_button_press(pane, None, event, record, check)

        self.assertTrue(handled)
        self.assertEqual(pane.selected_ids, {"existing"})
        self.assertFalse(check.get_active())
        self.assertEqual(menus, [])

    def test_right_click_opens_menu_without_changing_selection(self) -> None:
        pane, menus = self._pane()
        record = SimpleNamespace(id="target")
        check = CheckStub()
        event = SimpleNamespace(
            button=3,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_session_button_press(pane, None, event, record, check)

        self.assertTrue(handled)
        self.assertEqual(pane.selected_ids, {"existing"})
        self.assertFalse(check.get_active())
        self.assertEqual(menus, [("target", check, event)])

    def test_single_click_row_activation_opens_that_session(self) -> None:
        opened = []
        record = SimpleNamespace(id="target")
        pane = SimpleNamespace(window=SimpleNamespace(open_session=opened.append))
        row = SimpleNamespace(_termdeck_session_record=record)

        SidePane._on_session_row_activated(pane, None, row)

        self.assertEqual(opened, [record])

    def test_non_session_row_activation_is_ignored(self) -> None:
        opened = []
        pane = SimpleNamespace(window=SimpleNamespace(open_session=opened.append))

        SidePane._on_session_row_activated(pane, None, SimpleNamespace())

        self.assertEqual(opened, [])

    def test_plain_left_click_on_card_opens_that_session(self) -> None:
        opened = []
        record = SimpleNamespace(id="target")
        pane = SimpleNamespace(
            window=SimpleNamespace(open_session=opened.append),
            _on_session_button_press=lambda *_args: False,
        )
        event = SimpleNamespace(button=1)

        handled = SidePane._on_session_card_press(
            pane,
            SimpleNamespace(),
            event,
            record,
            CheckStub(),
        )

        self.assertTrue(handled)
        self.assertEqual(opened, [record])

    def test_ctrl_click_card_remains_bulk_selection(self) -> None:
        opened = []
        delegated = []
        record = SimpleNamespace(id="target")
        pane = SimpleNamespace(
            window=SimpleNamespace(open_session=opened.append),
            _on_session_button_press=lambda *args: delegated.append(args) or True,
        )
        event = SimpleNamespace(
            button=1,
            state=int(gtk_app.Gdk.ModifierType.CONTROL_MASK),
        )

        handled = SidePane._on_session_card_press(
            pane,
            SimpleNamespace(),
            event,
            record,
            CheckStub(),
        )

        self.assertTrue(handled)
        self.assertEqual(len(delegated), 1)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
