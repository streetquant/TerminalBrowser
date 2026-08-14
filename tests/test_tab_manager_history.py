from types import SimpleNamespace
from unittest import TestCase
from unittest import mock

from termdeck.gtk_app import Gtk, KILLED_HISTORY_STATUSES, MasterWindow, _partition_master_rows
from termdeck.master_tasks import MasterTaskRow


class TabManagerHistoryTest(TestCase):
    def test_killed_lifecycle_states_move_to_history(self) -> None:
        records = [
            SimpleNamespace(id="live", status="running"),
            SimpleNamespace(id="paused", status="paused"),
            SimpleNamespace(id="new", status="new"),
            *[
                SimpleNamespace(id=status, status=status)
                for status in sorted(KILLED_HISTORY_STATUSES)
            ],
        ]
        rows = [SimpleNamespace(session_id=record.id) for record in records]

        active, history = _partition_master_rows(records, rows)

        self.assertEqual(
            [row.session_id for row in active],
            ["live", "paused", "new"],
        )
        self.assertEqual(
            {row.session_id for row in history},
            KILLED_HISTORY_STATUSES,
        )

    def test_partition_uses_persisted_status_not_rendered_health(self) -> None:
        records = [SimpleNamespace(id="missing-live", status="running")]
        rows = [SimpleNamespace(session_id="missing-live", state="stopped")]

        active, history = _partition_master_rows(records, rows)

        self.assertEqual(active, rows)
        self.assertEqual(history, [])

    def test_manager_exposes_persistent_tabs_and_killed_history_pages(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        live = SimpleNamespace(id="live", status="running", pinned=False, name="Live")
        killed = SimpleNamespace(id="killed", status="stopped", pinned=False, name="Killed")
        records = [live, killed]

        def row(record) -> MasterTaskRow:
            return MasterTaskRow(
                session_id=record.id,
                window_title="Window",
                name=record.name,
                tag="work",
                pin="",
                pid="",
                state=record.status,
                health="",
                cpu="",
                ram="",
                process_count="",
                runtime="",
                focus_key="",
                pause_key="",
                command="bash",
                cwd="/tmp",
            )

        context = SimpleNamespace(
            settings=SimpleNamespace(
                ui_density="comfortable",
                ui_refresh_interval_seconds=5.0,
            ),
            store=SimpleNamespace(
                list=lambda: records,
                get=lambda session_id: next(
                    (record for record in records if record.id == session_id), None
                ),
            ),
            backend=SimpleNamespace(capture=lambda *_args, **_kwargs: ""),
            sampler=SimpleNamespace(),
            budget=SimpleNamespace(),
            windows=[],
        )
        items = [SimpleNamespace(row=row(record)) for record in records]
        # MasterWindow consumes the application-level shared snapshot memo.
        context.shared_task_snapshot = lambda *, force=False: (records, items)
        with mock.patch("termdeck.gtk_app.build_task_snapshot", return_value=items), mock.patch(
            "termdeck.gtk_app.GLib.timeout_add_seconds", return_value=1
        ):
            manager = MasterWindow(context)
            try:
                labels = [
                    manager.view_notebook.get_tab_label_text(
                        manager.view_notebook.get_nth_page(index)
                    )
                    for index in range(manager.view_notebook.get_n_pages())
                ]
                self.assertEqual(labels, ["Tabs", "Killed history"])
                self.assertEqual([model_row[0] for model_row in manager.store], ["live"])
                self.assertEqual(
                    [model_row[0] for model_row in manager.history_store], ["killed"]
                )

                manager.refresh()
                self.assertEqual(
                    [model_row[0] for model_row in manager.history_store], ["killed"]
                )
            finally:
                manager.destroy()
