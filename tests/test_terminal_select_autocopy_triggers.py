from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import AttachTerminal


class TerminalStub:
    def __init__(self, has_selection=True) -> None:
        self.has_selection = has_selection

    def get_has_selection(self) -> bool:
        return self.has_selection


class TerminalSelectAutocopyTriggersTest(TestCase):
    def test_selection_changed_with_selection_debounces_copy(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=TerminalStub(has_selection=True),
            _selection_pending=False,
            _queue_selection_copy=lambda **kwargs: calls.append(kwargs),
        )

        AttachTerminal._on_selection_changed(attach, None)

        self.assertTrue(attach._selection_pending)
        self.assertEqual(calls, [{"delay_ms": 120}])

    def test_selection_changed_without_selection_does_not_queue_copy(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=TerminalStub(has_selection=False),
            _selection_pending=True,
            _queue_selection_copy=lambda **kwargs: calls.append(kwargs),
        )

        AttachTerminal._on_selection_changed(attach, None)

        self.assertFalse(attach._selection_pending)
        self.assertEqual(calls, [])

    def test_queue_selection_copy_replaces_pending_timeout(self) -> None:
        removed = []
        timeouts = []
        attach = SimpleNamespace(
            _selection_autocopy_source_id=41,
            _copy_selection_to_clipboards_idle=lambda: False,
        )

        with patch("termdeck.gtk_app.GLib.source_remove", lambda source_id: removed.append(source_id)):
            with patch("termdeck.gtk_app.GLib.timeout_add", lambda delay, callback: timeouts.append((delay, callback)) or 99):
                AttachTerminal._queue_selection_copy(attach, delay_ms=75)

        self.assertEqual(removed, [41])
        self.assertEqual(timeouts, [(75, attach._copy_selection_to_clipboards_idle)])
        self.assertEqual(attach._selection_autocopy_source_id, 99)

    def test_queue_selection_copy_clears_source_when_timeout_add_fails(self) -> None:
        removed = []
        attach = SimpleNamespace(
            _selection_autocopy_source_id=12,
            _copy_selection_to_clipboards_idle=lambda: False,
        )

        def fail_timeout(_delay, _callback):
            raise RuntimeError("no main loop")

        with patch("termdeck.gtk_app.GLib.source_remove", lambda source_id: removed.append(source_id)):
            with patch("termdeck.gtk_app.GLib.timeout_add", fail_timeout):
                AttachTerminal._queue_selection_copy(attach, delay_ms=50)

        self.assertEqual(removed, [12])
        self.assertEqual(attach._selection_autocopy_source_id, 0)

    def test_left_button_release_cancels_pending_debounce_and_queues_idle_copy(self) -> None:
        removed = []
        idle_callbacks = []
        attach = SimpleNamespace(
            terminal=TerminalStub(has_selection=True),
            _selection_autocopy_source_id=33,
            _copy_selection_to_clipboards_idle=lambda: False,
        )
        event = SimpleNamespace(button=1)

        with patch("termdeck.gtk_app.GLib.source_remove", lambda source_id: removed.append(source_id)):
            with patch("termdeck.gtk_app.GLib.idle_add", lambda callback: idle_callbacks.append(callback) or 77):
                handled = AttachTerminal._on_button_release(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(removed, [33])
        self.assertEqual(attach._selection_autocopy_source_id, 0)
        self.assertEqual(idle_callbacks, [attach._copy_selection_to_clipboards_idle])

    def test_non_left_button_release_does_not_queue_idle_copy(self) -> None:
        idle_callbacks = []
        attach = SimpleNamespace(
            terminal=TerminalStub(has_selection=False),
            _selection_autocopy_source_id=0,
            _copy_selection_to_clipboards_idle=lambda: False,
            prepare_for_live_input=lambda: None,
            scroll_to_bottom=lambda: None,
        )
        event = SimpleNamespace(button=3, state=0)

        with patch("termdeck.gtk_app.GLib.idle_add", lambda callback: idle_callbacks.append(callback) or 77):
            handled = AttachTerminal._on_button_release(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(idle_callbacks, [])

    def test_idle_copy_resets_pending_state_after_copy(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _selection_autocopy_source_id=81,
            _selection_pending=True,
            _copy_selection_to_clipboards=lambda: calls.append("copy"),
        )

        keep_running = AttachTerminal._copy_selection_to_clipboards_idle(attach)

        self.assertFalse(keep_running)
        self.assertEqual(attach._selection_autocopy_source_id, 0)
        self.assertFalse(attach._selection_pending)
        self.assertEqual(calls, ["copy"])


if __name__ == "__main__":
    import unittest

    unittest.main()
