from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import SidePane
from termdeck.models import SessionRecord


def make_record(session_id: str, name: str) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=name,
        tag="work",
        command="bash",
        cwd="/tmp",
        tmux_name=f"termdeck-{session_id}",
        created_at="2026-07-09T00:00:00Z",
        last_opened_at="2026-07-09T00:00:00Z",
        status="running",
        last_pid=1234,
    )


class SidePaneBulkContextTest(TestCase):
    def test_context_records_uses_selected_group_when_target_is_selected(self) -> None:
        alpha = make_record("alpha", "alpha")
        beta = make_record("beta", "beta")
        pane = SimpleNamespace(
            selected_ids={"alpha", "beta"},
            selected_records=lambda: [alpha, beta],
        )

        records = SidePane._context_records(pane, alpha)

        self.assertEqual(records, [alpha, beta])

    def test_context_records_uses_singleton_when_target_is_not_selected(self) -> None:
        alpha = make_record("alpha", "alpha")
        beta = make_record("beta", "beta")
        pane = SimpleNamespace(
            selected_ids={"beta"},
            selected_records=lambda: [beta],
        )

        records = SidePane._context_records(pane, alpha)

        self.assertEqual(records, [alpha])

    def test_context_records_falls_back_to_singleton_when_selection_is_stale(self) -> None:
        alpha = make_record("alpha", "alpha")
        pane = SimpleNamespace(
            selected_ids={"alpha"},
            selected_records=lambda: [],
        )

        records = SidePane._context_records(pane, alpha)

        self.assertEqual(records, [alpha])

    def test_context_label_switches_to_bulk_wording_only_for_multiple_records(self) -> None:
        pane = SimpleNamespace()
        one = [make_record("one", "one")]
        many = [make_record("one", "one"), make_record("two", "two")]

        self.assertEqual(SidePane._context_label(pane, "Hide", one), "Hide")
        self.assertEqual(SidePane._context_label(pane, "Hide", many), "Hide selected (2)")


if __name__ == "__main__":
    import unittest

    unittest.main()
