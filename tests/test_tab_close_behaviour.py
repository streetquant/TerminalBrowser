import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import TermDeckWindow
from termdeck.session_store import SessionStore


class TabCloseBehaviourTest(TestCase):
    def test_pinned_close_button_shows_guidance_without_hiding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = store.create("pinned", "dev", "bash", tmp, tab_index=0)
            store.update(record.id, pinned=True)
            hidden = []
            toasts = []
            window = SimpleNamespace(
                app_context=SimpleNamespace(store=store),
                hide_tab=lambda session_id, **_kwargs: hidden.append(session_id),
                show_toast=lambda text, **_kwargs: toasts.append(text),
            )

            TermDeckWindow._hide_tab_from_close_button(window, record.id)

            self.assertEqual(hidden, [])
            self.assertEqual(toasts, ["Pinned tab: right-click and choose Unpin and hide tab"])
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


if __name__ == "__main__":
    import unittest
    unittest.main()
