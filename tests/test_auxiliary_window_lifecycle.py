from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import TermDeckWindow


class WindowStub:
    def __init__(self, *, realized: bool = True, visible: bool = True) -> None:
        self.realized = realized
        self.visible = visible
        self.calls = []
        self.callbacks = {}

    def get_realized(self) -> bool:
        return self.realized

    def get_visible(self) -> bool:
        return self.visible

    def refresh(self) -> None:
        self.calls.append("refresh")

    def present(self) -> None:
        self.calls.append("present")

    def show_all(self) -> None:
        self.calls.append("show_all")

    def connect(self, signal: str, callback) -> None:
        self.calls.append(("connect", signal))
        self.callbacks[signal] = callback

    def _redistribute_column_widths(self) -> None:
        self.calls.append("redistribute")

    def maximize(self) -> None:
        self.calls.append("maximize")


class ExplodingWindowStub(WindowStub):
    def get_realized(self) -> bool:
        raise RuntimeError("stale window")


class AuxiliaryWindowLifecycleTest(TestCase):
    def test_open_master_window_reuses_visible_window(self) -> None:
        existing = WindowStub(realized=True, visible=True)
        window = SimpleNamespace(master_window=existing, app_context=object())

        with patch("termdeck.gtk_app.MasterWindow") as master_cls:
            TermDeckWindow.open_master_window(window)

        master_cls.assert_not_called()
        self.assertIs(window.master_window, existing)
        self.assertEqual(existing.calls, ["refresh", "present"])

    def test_open_master_window_destroy_callback_clears_reference(self) -> None:
        created = WindowStub()
        window = SimpleNamespace(master_window=None, app_context=object())

        with patch("termdeck.gtk_app.MasterWindow", return_value=created):
            TermDeckWindow.open_master_window(window)

        self.assertIs(window.master_window, created)
        self.assertIn("destroy", created.callbacks)
        self.assertEqual(created.calls, [("connect", "destroy"), "show_all", "present"])

        created.callbacks["destroy"]()

        self.assertIsNone(window.master_window)

    def test_open_search_window_rebuilds_stale_reference(self) -> None:
        stale = ExplodingWindowStub()
        created = WindowStub()
        window = SimpleNamespace(search_window=stale, app_context=object())

        with patch("termdeck.gtk_app.SessionSearchWindow", return_value=created):
            TermDeckWindow.open_search_window(window)

        self.assertIs(window.search_window, created)
        self.assertEqual(created.calls, [("connect", "destroy"), "show_all", "present"])
        created.callbacks["destroy"]()
        self.assertIsNone(window.search_window)

    def test_open_window_overview_reuses_visible_window_without_rebuild(self) -> None:
        existing = WindowStub(realized=True, visible=True)
        window = SimpleNamespace(window_overview=existing, app_context=object())

        with patch("termdeck.gtk_app.WindowOverview") as overview_cls:
            TermDeckWindow.open_window_overview(window)

        overview_cls.assert_not_called()
        self.assertIs(window.window_overview, existing)
        self.assertEqual(existing.calls, ["present"])

    def test_open_shortcut_window_sets_parent_maximizes_and_schedules_layout(self) -> None:
        created = WindowStub()
        window = SimpleNamespace(shortcut_window=None, app_context=object(), is_maximized=lambda: True)
        idle_calls = []

        with patch("termdeck.gtk_app.ShortcutWindow", return_value=created) as shortcut_cls, patch(
            "termdeck.gtk_app.GLib.idle_add", side_effect=lambda callback: idle_calls.append(callback)
        ) as idle_add:
            TermDeckWindow.open_shortcut_window(window)

        shortcut_cls.assert_called_once_with(window.app_context, parent=window)
        idle_add.assert_called_once_with(created._redistribute_column_widths)
        self.assertIs(window.shortcut_window, created)
        self.assertEqual(created.calls, [("connect", "destroy"), "show_all", "present", "maximize"])
        self.assertEqual(idle_calls, [created._redistribute_column_widths])

        created.callbacks["destroy"]()

        self.assertIsNone(window.shortcut_window)


if __name__ == "__main__":
    import unittest

    unittest.main()
