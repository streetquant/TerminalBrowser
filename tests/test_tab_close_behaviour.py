import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckApplication, TermDeckWindow
from termdeck.models import RECOVERY_INTENT_INACTIVE
from termdeck.session_store import SessionStore


class TabCloseBehaviourTest(TestCase):
    def test_pinned_close_button_closes_tab_without_unpinning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = store.create("pinned", "dev", "bash", tmp, tab_index=0)
            store.update(record.id, pinned=True)
            hidden = []
            window = SimpleNamespace(
                app_context=SimpleNamespace(store=store),
                hide_tab=lambda session_id, **kwargs: hidden.append((session_id, kwargs)),
            )

            TermDeckWindow._hide_tab_from_close_button(window, record.id)

            self.assertEqual(hidden, [(record.id, {"include_pinned": True})])
            self.assertTrue(store.get(record.id).pinned)
            store.close()

    def test_unpin_and_hide_tab_updates_store_and_bypasses_pinned_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = store.create("pinned", "dev", "bash", tmp, tab_index=0)
            pinned = store.update(record.id, pinned=True)
            hidden = []
            toasts = []
            window = SimpleNamespace(
                app_context=SimpleNamespace(store=store),
                hide_tab=lambda session_id, **kwargs: hidden.append((session_id, kwargs)),
                show_toast=lambda text, **_kwargs: toasts.append(text),
            )

            TermDeckWindow._unpin_and_hide_tab(window, pinned)

            self.assertFalse(store.get(record.id).pinned)
            self.assertEqual(hidden, [(record.id, {"include_pinned": True})])
            self.assertEqual(toasts, ["Unpinned and hid tab"])
            store.close()

    def test_close_session_tabs_removes_target_from_every_window(self) -> None:
        calls = []

        def make_window(name: str, removable: bool):
            side = SimpleNamespace(
                selected_ids={"keep", "target"},
                refresh=lambda: calls.append((name, "refresh")),
            )
            return SimpleNamespace(
                side=side,
                _remove_tab_widget=lambda session_id: calls.append((name, "remove", session_id)) or removable,
                _renumber_visible_tabs=lambda: calls.append((name, "renumber")),
            )

        first = make_window("first", True)
        second = make_window("second", False)
        app = SimpleNamespace(windows=[first, second])

        TermDeckApplication.close_session_tabs(app, {"target"})

        self.assertNotIn("target", first.side.selected_ids)
        self.assertNotIn("target", second.side.selected_ids)
        self.assertIn(("first", "renumber"), calls)
        self.assertNotIn(("second", "renumber"), calls)
        self.assertIn(("first", "refresh"), calls)
        self.assertIn(("second", "refresh"), calls)

    def test_terminate_records_batches_registry_and_cross_window_cleanup(self) -> None:
        calls = []
        first = SimpleNamespace(id="first")
        second = SimpleNamespace(id="second")
        app = SimpleNamespace(
            backend=SimpleNamespace(terminate=lambda record: calls.append(("terminate", record.id))),
            store=SimpleNamespace(
                update=lambda session_id, **changes: calls.append(("update", session_id, changes))
            ),
            close_session_tabs=lambda session_ids: calls.append(("close", session_ids)),
        )

        count = TermDeckApplication.terminate_records(app, [first, second, first])

        self.assertEqual(count, 2)
        self.assertEqual(calls[0], ("terminate", "first"))
        self.assertEqual(
            calls[1],
            (
                "update",
                "first",
                {
                    "status": "stopped",
                    "visible": False,
                    "last_pid": None,
                    "recovery_command": None,
                    "recovery_cwd": None,
                    "recovery_intent": RECOVERY_INTENT_INACTIVE,
                },
            ),
        )
        self.assertEqual(calls[2], ("terminate", "second"))
        self.assertEqual(
            calls[3],
            (
                "update",
                "second",
                {
                    "status": "stopped",
                    "visible": False,
                    "last_pid": None,
                    "recovery_command": None,
                    "recovery_cwd": None,
                    "recovery_intent": RECOVERY_INTENT_INACTIVE,
                },
            ),
        )
        self.assertEqual(calls[4], ("close", {"first", "second"}))


if __name__ == "__main__":
    import unittest
    unittest.main()
