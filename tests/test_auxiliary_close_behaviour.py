from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import _wire_auxiliary_close


class WindowStub:
    def __init__(self, *, hide_raises: bool = False) -> None:
        self.hide_raises = hide_raises
        self.handlers = {}
        self.calls = []

    def connect(self, signal_name: str, handler) -> None:
        self.handlers[signal_name] = handler

    def hide(self) -> None:
        self.calls.append("hide")
        if self.hide_raises:
            raise RuntimeError("hide failed")

    def destroy(self) -> None:
        self.calls.append("destroy")


class AuxiliaryCloseBehaviourTest(TestCase):
    def test_delete_event_hides_and_destroys_window_and_consumes_event(self) -> None:
        window = WindowStub()

        _wire_auxiliary_close(window)
        handled = window.handlers["delete-event"](window, object())

        self.assertTrue(handled)
        self.assertEqual(window.calls, ["hide", "destroy"])

    def test_escape_key_hides_and_destroys_window_and_consumes_event(self) -> None:
        window = WindowStub()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_Escape)

        _wire_auxiliary_close(window)
        handled = window.handlers["key-press-event"](window, event)

        self.assertTrue(handled)
        self.assertEqual(window.calls, ["hide", "destroy"])

    def test_non_escape_key_does_not_close_or_consume_event(self) -> None:
        window = WindowStub()
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_Return)

        _wire_auxiliary_close(window)
        handled = window.handlers["key-press-event"](window, event)

        self.assertFalse(handled)
        self.assertEqual(window.calls, [])

    def test_destroy_still_runs_if_hide_raises(self) -> None:
        window = WindowStub(hide_raises=True)

        _wire_auxiliary_close(window)
        with self.assertRaises(RuntimeError):
            window.handlers["delete-event"](window, object())

        self.assertEqual(window.calls, ["hide", "destroy"])


if __name__ == "__main__":
    import unittest

    unittest.main()
