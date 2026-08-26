import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from termdeck.gtk_app import SidePane, TermDeckApplication, TermDeckWindow, _tabs_pane_records
from termdeck.session_store import SessionStore
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
    def test_hidden_pane_refresh_skips_store_backend_and_widget_work(self) -> None:
        pane = SimpleNamespace(
            get_visible=lambda: False,
            _last_refresh_signature=("stale",),
        )

        SidePane.refresh(pane)

        self.assertIsNone(pane._last_refresh_signature)

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
        window._apply_side_pane_visibility = lambda: (
            side.set_visible(window.tabs_pane_visible),
            events.append(("layout", "stable")),
        )

        TermDeckWindow.set_tabs_pane_visible(window, True)

        self.assertTrue(window.tabs_pane_visible)
        self.assertTrue(side.visible)
        self.assertEqual(side.refresh_count, 1)
        self.assertEqual(
            events,
            [
                ("layout", "stable"),
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
        window._apply_side_pane_visibility = lambda: (
            side.set_visible(window.tabs_pane_visible),
            events.append(("layout", "stable")),
        )

        TermDeckWindow.set_tabs_pane_visible(window, False, persist=False)

        self.assertFalse(window.tabs_pane_visible)
        self.assertFalse(side.visible)
        self.assertEqual(side.refresh_count, 0)
        self.assertEqual(
            events,
            [
                ("layout", "stable"),
                ("toast", "Tabs pane hidden", {"kind": "info"}),
            ],
        )

    def test_toggle_uses_the_window_visibility_state(self) -> None:
        calls = []
        window = SimpleNamespace(
            tabs_pane_visible=False,
            set_tabs_pane_visible=lambda visible: calls.append(visible),
        )

        TermDeckWindow.toggle_tabs_pane(window)

        self.assertEqual(calls, [True])

    def test_tabs_pane_records_only_include_the_current_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                mine = store.create(
                    "shell-a",
                    "terminal",
                    "bash",
                    tmp,
                    window_id="win-a",
                    window_title="A",
                    tab_index=0,
                    visible=True,
                )
                store.create(
                    "shell-b",
                    "terminal",
                    "bash",
                    tmp,
                    window_id="win-b",
                    window_title="B",
                    tab_index=0,
                    visible=True,
                )
                store.create(
                    "hidden-a",
                    "terminal",
                    "bash",
                    tmp,
                    window_id="win-a",
                    window_title="A",
                    tab_index=1,
                    visible=False,
                )

                rows = _tabs_pane_records(store, "win-a")

                self.assertEqual([row.id for row in rows], [mine.id])
            finally:
                store.close()


    def test_open_notebook_tab_is_not_dropped_when_codex_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create(
                    "codex-shell",
                    "terminal",
                    "bash",
                    tmp,
                    window_id="win-a",
                    window_title="A",
                    tab_index=0,
                    visible=True,
                )
                store.update(record.id, status="stopped", last_pid=None)

                hidden = _tabs_pane_records(store, "win-a")
                open_rows = _tabs_pane_records(
                    store,
                    "win-a",
                    open_session_ids={record.id},
                )

                self.assertEqual(hidden, [])
                self.assertEqual([row.id for row in open_rows], [record.id])
            finally:
                store.close()

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
