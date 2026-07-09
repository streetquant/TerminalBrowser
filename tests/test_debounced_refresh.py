from unittest import TestCase

from termdeck import gtk_app


class DummyWidget:
    def __init__(self) -> None:
        self.handlers = {}

    def connect(self, signal_name: str, handler) -> None:
        self.handlers[signal_name] = handler

    def emit(self, signal_name: str) -> None:
        self.handlers[signal_name](self)


class DebouncedRefreshTest(TestCase):
    def setUp(self) -> None:
        self._timeout_add = gtk_app.GLib.timeout_add
        self._source_remove = gtk_app.GLib.source_remove

    def tearDown(self) -> None:
        gtk_app.GLib.timeout_add = self._timeout_add
        gtk_app.GLib.source_remove = self._source_remove

    def test_coalesces_repeated_search_changes_into_one_refresh(self) -> None:
        widget = DummyWidget()
        refresh_calls = []
        scheduled = {}
        removed = []
        next_source = 10

        def add_timer(delay_ms, callback):
            nonlocal next_source
            source_id = next_source
            next_source += 1
            scheduled[source_id] = (delay_ms, callback)
            return source_id

        gtk_app.GLib.timeout_add = add_timer
        gtk_app.GLib.source_remove = removed.append

        gtk_app._connect_debounced_refresh(widget, lambda: refresh_calls.append("refresh"), delay_ms=140)
        widget.emit("search-changed")
        widget.emit("search-changed")

        self.assertEqual(refresh_calls, [])
        self.assertEqual(removed, [10])
        self.assertEqual(set(scheduled), {10, 11})
        self.assertEqual(scheduled[11][0], 140)

        keep_running = scheduled[11][1]()

        self.assertFalse(keep_running)
        self.assertEqual(refresh_calls, ["refresh"])

    def test_destroy_cleans_pending_refresh_timer(self) -> None:
        widget = DummyWidget()
        removed = []
        gtk_app.GLib.timeout_add = lambda _delay_ms, _callback: 22
        gtk_app.GLib.source_remove = removed.append

        gtk_app._connect_debounced_refresh(widget, lambda: None, delay_ms=140)
        widget.emit("search-changed")
        widget.emit("destroy")

        self.assertEqual(removed, [22])


if __name__ == "__main__":
    import unittest
    unittest.main()
