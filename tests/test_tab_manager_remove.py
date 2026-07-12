from types import SimpleNamespace
from unittest import TestCase

from termdeck.gtk_app import MasterWindow
from termdeck.models import SessionRecord


def make_record(
    session_id: str,
    *,
    status: str = "stopped",
    pinned: bool = False,
    visible: bool = True,
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name=session_id,
        tag="work",
        command="bash",
        cwd="/tmp",
        tmux_name=f"termdeck-{session_id}",
        created_at="2026-07-09T00:00:00Z",
        last_opened_at="2026-07-09T00:00:00Z",
        status=status,
        last_pid=None,
        pinned=pinned,
        visible=visible,
    )


class TabManagerRemoveTest(TestCase):
    def test_remove_selected_deletes_stopped_records_from_store(self) -> None:
        stopped = make_record("stopped-tab", status="stopped", visible=False)
        deleted: list[str] = []
        refreshed: list[str] = []
        store = SimpleNamespace(
            get=lambda session_id: stopped if session_id == stopped.id else None,
            delete=lambda session_id: deleted.append(session_id),
        )
        master = SimpleNamespace(
            selected_records=lambda: [stopped],
            app_context=SimpleNamespace(windows=[], store=store),
            refresh=lambda: refreshed.append("refresh"),
        )

        MasterWindow.remove_selected(master)

        self.assertEqual(deleted, [stopped.id])
        self.assertEqual(refreshed, ["refresh"])

    def test_remove_selected_rejects_live_or_unconfirmed_records(self) -> None:
        removed: list[str] = []
        renumbered: list[str] = []
        side_refreshed: list[str] = []
        deleted: list[str] = []
        window = SimpleNamespace(
            tabs={status: object() for status in ("running", "paused", "new", "missing")},
            _remove_tab_widget=lambda session_id: removed.append(session_id) or True,
            _renumber_visible_tabs=lambda: renumbered.append("renumber"),
            side=SimpleNamespace(refresh=lambda: side_refreshed.append("side")),
        )
        store = SimpleNamespace(delete=lambda session_id: deleted.append(session_id))
        for status in ("running", "paused", "new", "missing"):
            record = make_record(status, status=status)
            master = SimpleNamespace(
                selected_records=lambda candidate=record: [candidate],
                app_context=SimpleNamespace(windows=[window], store=store),
                refresh=lambda: None,
            )

            MasterWindow.remove_selected(master)

        self.assertEqual(removed, [])
        self.assertEqual(renumbered, [])
        self.assertEqual(side_refreshed, [])
        self.assertEqual(deleted, [])

    def test_remove_selected_skips_pinned_tabs(self) -> None:
        pinned = make_record("pinned", pinned=True, status="stopped")
        deleted: list[str] = []
        store = SimpleNamespace(delete=lambda session_id: deleted.append(session_id))
        master = SimpleNamespace(
            selected_records=lambda: [pinned],
            app_context=SimpleNamespace(windows=[], store=store),
            refresh=lambda: None,
        )

        MasterWindow.remove_selected(master)

        self.assertEqual(deleted, [])

    def test_remove_button_requires_only_unpinned_stopped_records(self) -> None:
        sensitivity: list[bool] = []
        master = SimpleNamespace(
            remove_button=SimpleNamespace(set_sensitive=lambda value: sensitivity.append(value)),
            selected_records=lambda: [make_record("stopped", status="stopped")],
        )

        MasterWindow.update_remove_sensitivity(master)
        master.selected_records = lambda: [make_record("running", status="running")]
        MasterWindow.update_remove_sensitivity(master)
        master.selected_records = lambda: [make_record("pinned", status="stopped", pinned=True)]
        MasterWindow.update_remove_sensitivity(master)
        master.selected_records = lambda: []
        MasterWindow.update_remove_sensitivity(master)

        self.assertEqual(sensitivity, [True, False, False, False])

    def test_hide_selected_alias_points_at_remove(self) -> None:
        self.assertIs(MasterWindow.hide_selected, MasterWindow.remove_selected)


if __name__ == "__main__":
    import unittest

    unittest.main()
