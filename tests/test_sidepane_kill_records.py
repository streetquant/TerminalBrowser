from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import SidePane


def make_record(session_id: str):
    return SimpleNamespace(id=session_id)


class ConfirmingAppContext:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.confirm_calls = []

    def confirm_control_plan(self, window, records, action: str, **kwargs) -> bool:
        self.confirm_calls.append((window, records, action, kwargs))
        return self.allowed


class SidePaneKillRecordsTest(TestCase):
    def test_kill_records_confirms_then_stops_each_session_and_cleans_selection(self) -> None:
        first = make_record("first")
        second = make_record("second")
        app_context = ConfirmingAppContext(True)
        stopped = []
        toasts = []
        refresh_calls = []
        window = SimpleNamespace(
            app_context=app_context,
            stop_session=lambda record: stopped.append(record.id),
            show_toast=lambda text, **kwargs: toasts.append((text, kwargs)),
        )
        pane = SimpleNamespace(
            window=window,
            selected_ids={"first", "second", "stale"},
            refresh=lambda: refresh_calls.append("refresh"),
        )

        SidePane._kill_records(pane, [first, second])

        self.assertEqual(app_context.confirm_calls, [(window, [first, second], "kill", {"selection": "side pane context menu"})])
        self.assertEqual(stopped, ["first", "second"])
        self.assertEqual(pane.selected_ids, {"stale"})
        self.assertEqual(refresh_calls, ["refresh"])
        self.assertEqual(toasts, [("Killed 2 sessions", {"kind": "danger"})])

    def test_kill_records_does_nothing_when_confirmation_is_denied(self) -> None:
        record = make_record("target")
        app_context = ConfirmingAppContext(False)
        stopped = []
        toasts = []
        refresh_calls = []
        window = SimpleNamespace(
            app_context=app_context,
            stop_session=lambda row: stopped.append(row.id),
            show_toast=lambda text, **kwargs: toasts.append((text, kwargs)),
        )
        pane = SimpleNamespace(
            window=window,
            selected_ids={"target"},
            refresh=lambda: refresh_calls.append("refresh"),
        )

        SidePane._kill_records(pane, [record])

        self.assertEqual(app_context.confirm_calls, [(window, [record], "kill", {"selection": "side pane context menu"})])
        self.assertEqual(stopped, [])
        self.assertEqual(pane.selected_ids, {"target"})
        self.assertEqual(refresh_calls, [])
        self.assertEqual(toasts, [])

    def test_kill_records_allows_empty_action_to_confirm_without_toast(self) -> None:
        app_context = ConfirmingAppContext(True)
        refresh_calls = []
        window = SimpleNamespace(
            app_context=app_context,
            stop_session=lambda record: None,
            show_toast=lambda text, **kwargs: None,
        )
        pane = SimpleNamespace(
            window=window,
            selected_ids={"stale"},
            refresh=lambda: refresh_calls.append("refresh"),
        )

        SidePane._kill_records(pane, [])

        self.assertEqual(app_context.confirm_calls, [(window, [], "kill", {"selection": "side pane context menu"})])
        self.assertEqual(pane.selected_ids, {"stale"})
        self.assertEqual(refresh_calls, ["refresh"])


if __name__ == "__main__":
    import unittest

    unittest.main()
