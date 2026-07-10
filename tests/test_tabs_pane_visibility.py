import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from termdeck.gtk_app import TermDeckApplication, TermDeckWindow
from termdeck.settings import AppSettings


class SideStub:
    def __init__(self) -> None:
        self.visible = None
        self.refresh_count = 0

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)

    def refresh(self) -> None:
        self.refresh_count += 1


class TabsPaneVisibilityTests(unittest.TestCase):
    def test_showing_tabs_pane_persists_and_refreshes(self) -> None:
        events = []
        side = SideStub()
        app_context = SimpleNamespace(
            apply_tabs_pane_visibility=lambda visible: events.append(("persist", visible))
        )
        window = SimpleNamespace(
            tabs_pane_visible=False,
            side=side,
            app_context=app_context,
            show_toast=lambda message, **kwargs: events.append(("toast", message, kwargs)),
        )

        TermDeckWindow.set_tabs_pane_visible(window, True)

        self.assertTrue(window.tabs_pane_visible)
        self.assertTrue(side.visible)
        self.assertEqual(side.refresh_count, 1)
        self.assertEqual(
            events,
            [
                ("persist", True),
                ("toast", "Tabs pane shown", {"kind": "info"}),
            ],
        )

    def test_hiding_tabs_pane_can_sync_without_persisting(self) -> None:
        events = []
        side = SideStub()
        app_context = SimpleNamespace(
            apply_tabs_pane_visibility=lambda visible: events.append(("persist", visible))
        )
        window = SimpleNamespace(
            tabs_pane_visible=True,
            side=side,
            app_context=app_context,
            show_toast=lambda message, **kwargs: events.append(("toast", message, kwargs)),
        )

        TermDeckWindow.set_tabs_pane_visible(window, False, persist=False)

        self.assertFalse(window.tabs_pane_visible)
        self.assertFalse(side.visible)
        self.assertEqual(side.refresh_count, 0)
        self.assertEqual(events, [("toast", "Tabs pane hidden", {"kind": "info"})])

    def test_toggle_uses_the_window_visibility_state(self) -> None:
        calls = []
        window = SimpleNamespace(
            tabs_pane_visible=False,
            set_tabs_pane_visible=lambda visible: calls.append(visible),
        )

        TermDeckWindow.toggle_tabs_pane(window)

        self.assertEqual(calls, [True])

    def test_application_persists_and_synchronises_all_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"appearance": {"focus_mode": False}}),
                encoding="utf-8",
            )
            calls = []
            first = SimpleNamespace(
                tabs_pane_visible=True,
                set_tabs_pane_visible=lambda visible, persist=False, notify=True: calls.append(
                    ("first", visible, persist, notify)
                ),
            )
            second = SimpleNamespace(
                tabs_pane_visible=False,
                set_tabs_pane_visible=lambda visible, persist=False, notify=True: calls.append(
                    ("second", visible, persist, notify)
                ),
            )
            app = SimpleNamespace(
                settings=AppSettings.load(path),
                windows=[first, second],
            )

            TermDeckApplication.apply_tabs_pane_visibility(app, False)

            self.assertFalse(app.settings.tabs_pane_visible)
            self.assertEqual(
                calls,
                [
                    ("first", False, False, False),
                    ("second", False, False, False),
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["appearance"]["tabs_pane_visible"], False)
            self.assertNotIn("focus_mode", payload["appearance"])


if __name__ == "__main__":
    unittest.main()
