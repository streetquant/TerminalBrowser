from unittest import TestCase

from termdeck.gtk_app import SIDE_PANE_HIDDEN_STATUSES, _show_in_sessions_pane
from termdeck.models import SessionRecord


def make_record(status: str, *, visible: bool = True) -> SessionRecord:
    return SessionRecord(
        id=f"session-{status}",
        name="dev",
        tag="work",
        command="bash",
        cwd="/tmp",
        tmux_name=f"termdeck-{status}",
        created_at="2026-07-09T00:00:00Z",
        last_opened_at="2026-07-09T00:00:00Z",
        status=status,
        last_pid=1234,
        visible=visible,
    )


class SessionsPaneVisibilityTest(TestCase):
    def test_non_live_statuses_are_hidden_from_sessions_pane(self) -> None:
        for status in ["stopped", "killed", "dead", "terminated", "missing", "archived"]:
            with self.subTest(status=status):
                self.assertFalse(_show_in_sessions_pane(make_record(status)))

    def test_hidden_status_filter_is_case_insensitive(self) -> None:
        self.assertFalse(_show_in_sessions_pane(make_record("KILLED")))

    def test_active_and_recoverable_statuses_stay_visible(self) -> None:
        for status in ["new", "running", "paused", "remembered", "start-error"]:
            with self.subTest(status=status):
                self.assertTrue(_show_in_sessions_pane(make_record(status)))

    def test_hidden_running_tabs_are_not_shown_in_sessions_pane(self) -> None:
        record = make_record("running", visible=False)

        self.assertFalse(_show_in_sessions_pane(record))

    def test_visible_flag_does_not_override_non_live_status(self) -> None:
        record = make_record("killed", visible=True)

        self.assertFalse(_show_in_sessions_pane(record))


    def test_open_terminal_tab_remains_visible_after_child_payload_stops(self) -> None:
        record = make_record("stopped", visible=True)

        self.assertTrue(
            _show_in_sessions_pane(record, open_session_ids={record.id})
        )

    def test_archived_row_remains_hidden_even_if_a_stale_widget_id_exists(self) -> None:
        record = make_record("archived", visible=True)

        self.assertFalse(
            _show_in_sessions_pane(record, open_session_ids={record.id})
        )

    def test_hidden_status_set_documents_kill_aliases(self) -> None:
        self.assertGreaterEqual(SIDE_PANE_HIDDEN_STATUSES, {"stopped", "killed", "dead", "terminated"})


if __name__ == "__main__":
    import unittest

    unittest.main()
