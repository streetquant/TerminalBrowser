import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from termdeck.session_lifecycle import archive_records, compact_window, hide_records, move_records, pin_records, recently_closed_records, rename_record, reopen_closed_records, retag_records, show_records, stop_records, unpin_records
from termdeck.session_store import SessionStore, utc_now


class SessionLifecycleTests(unittest.TestCase):
    def test_session_timestamps_have_fractional_precision_for_close_ordering(self):
        self.assertRegex(utc_now(), r"\.\d{6}\+00:00$")

    def test_hide_compacts_visible_tab_order_without_killing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "dev", "bash", tmp, window_id="win", window_title="Dev", tab_index=0)
                second = store.create("second", "dev", "bash", tmp, window_id="win", window_title="Dev", tab_index=1)
                third = store.create("third", "dev", "bash", tmp, window_id="win", window_title="Dev", tab_index=2)
                store.update(second.id, status="running", last_pid=1234)

                results = hide_records(store, [second])

                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].ok)
                hidden = store.get(second.id)
                self.assertFalse(hidden.visible)  # type: ignore[union-attr]
                self.assertEqual(hidden.status, "running")  # type: ignore[union-attr]
                visible = store.list(visible_only=True, window_id="win")
                self.assertEqual([record.id for record in visible], [first.id, third.id])
                self.assertEqual([record.tab_index for record in visible], [0, 1])
            finally:
                store.close()

    def test_show_places_hidden_records_in_target_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                existing = store.create("existing", "dev", "bash", tmp, window_id="target", window_title="Target", tab_index=0)
                hidden = store.create("hidden", "ai", "codex", tmp, visible=False, window_id="old", window_title="Old", tab_index=5)

                results = show_records(store, [hidden], window_id="target", window_title="Target")

                self.assertTrue(results[0].visible)
                updated = store.get(hidden.id)
                self.assertEqual(updated.window_id, "target")  # type: ignore[union-attr]
                self.assertEqual(updated.window_title, "Target")  # type: ignore[union-attr]
                self.assertEqual(updated.tab_index, 1)  # type: ignore[union-attr]
                visible_ids = [record.id for record in store.list(visible_only=True, window_id="target")]
                self.assertEqual(visible_ids, [existing.id, hidden.id])
            finally:
                store.close()

    def test_move_rename_retag_archive_are_registry_actions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("old", "scratch", "sleep 999", tmp, window_id="one", window_title="One", tab_index=0)
                store.update(record.id, status="running", last_pid=77)
                record = store.get(record.id)

                renamed = rename_record(store, record, "agent")  # type: ignore[arg-type]
                self.assertTrue(renamed.ok)
                record = store.get(record.id)  # type: ignore[union-attr]
                self.assertEqual(record.name, "agent")  # type: ignore[union-attr]
                self.assertEqual(record.last_pid, 77)  # type: ignore[union-attr]

                retag_records(store, [record], "ai")  # type: ignore[list-item]
                record = store.get(record.id)  # type: ignore[union-attr]
                self.assertEqual(record.tag, "ai")  # type: ignore[union-attr]

                move_records(store, [record], window_id="two", window_title="Two", tab_index=3)  # type: ignore[list-item]
                record = store.get(record.id)  # type: ignore[union-attr]
                self.assertEqual(record.window_id, "two")  # type: ignore[union-attr]
                self.assertEqual(record.window_title, "Two")  # type: ignore[union-attr]
                self.assertEqual(record.tab_index, 0)  # compacted in the new window
                self.assertTrue(record.visible)  # type: ignore[union-attr]

                archive_records(store, [record])  # type: ignore[list-item]
                archived = store.get(record.id)  # type: ignore[union-attr]
                self.assertEqual(archived.status, "archived")  # type: ignore[union-attr]
                self.assertFalse(archived.visible)  # type: ignore[union-attr]
                self.assertEqual(archived.last_pid, 77)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_explicit_stop_and_archive_disable_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                running = store.create("running", "dev", "bash", tmp)
                archived = store.create("archived", "dev", "bash", tmp)
                store.update(running.id, status="running", last_pid=101)
                store.update(archived.id, status="running", last_pid=202)

                stopped_result = stop_records(store, [store.get(running.id)])  # type: ignore[list-item]
                archive_records(store, [store.get(archived.id)])  # type: ignore[list-item]

                self.assertTrue(stopped_result[0].ok)
                stopped = store.get(running.id)
                archived_row = store.get(archived.id)
                self.assertEqual(stopped.status, "stopped")  # type: ignore[union-attr]
                self.assertEqual(stopped.recovery_intent, "inactive")  # type: ignore[union-attr]
                self.assertEqual(archived_row.status, "archived")  # type: ignore[union-attr]
                self.assertEqual(archived_row.recovery_intent, "inactive")  # type: ignore[union-attr]
            finally:
                store.close()

    def test_compact_window_only_changes_visible_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win", window_title="Win", tab_index=0, visible=False)
                later = store.create("later", "dev", "bash", tmp, window_id="win", window_title="Win", tab_index=9)
                compacted = compact_window(store, "win")
                self.assertEqual([record.id for record in compacted], [later.id])
                self.assertEqual(store.get(later.id).tab_index, 0)  # type: ignore[union-attr]
                self.assertEqual(store.get(hidden.id).tab_index, 0)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_pin_persists_and_protects_bulk_hide_until_unpinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                shell = store.create("shell", "dev", "bash", tmp, window_id="agents", window_title="Agents", tab_index=0)
                record = store.create("agent", "ai", "codex", tmp, window_id="agents", window_title="Agents", tab_index=1)
                pin_result = pin_records(store, [record])[0]
                self.assertTrue(pin_result.ok)
                pinned = store.get(record.id)
                self.assertTrue(pinned.pinned)  # type: ignore[union-attr]
                visible = store.list(visible_only=True, window_id="agents")
                self.assertEqual([row.id for row in visible], [record.id, shell.id])
                self.assertEqual([row.tab_index for row in visible], [0, 1])

                hidden = hide_records(store, [pinned])  # type: ignore[list-item]
                self.assertFalse(hidden[0].ok)
                self.assertTrue(store.get(record.id).visible)  # type: ignore[union-attr]

                unpin_records(store, [store.get(record.id)])  # type: ignore[list-item]
                hidden = hide_records(store, [store.get(record.id)])  # type: ignore[list-item]
                self.assertTrue(hidden[0].ok)
                self.assertFalse(store.get(record.id).visible)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_recently_closed_and_reopen_skip_stopped_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                running = store.create("agent", "ai", "codex", tmp, visible=False, window_id="agents", window_title="Agents", tab_index=2)
                stopped = store.create("old", "ai", "bash", tmp, visible=False, window_id="agents", window_title="Agents", tab_index=3)
                store.update(running.id, status="running", visible=False)
                store.update(stopped.id, status="stopped", visible=False)

                closed = recently_closed_records(store, tag="ai")
                self.assertEqual([record.id for record in closed], [running.id])

                reopened = reopen_closed_records(store, closed, window_id="main", window_title="Main")
                self.assertEqual(len(reopened), 1)
                self.assertEqual(reopened[0].action, "reopen-closed")
                refreshed = store.get(running.id)
                self.assertTrue(refreshed.visible)  # type: ignore[union-attr]
                self.assertEqual(refreshed.window_title, "Main")  # type: ignore[union-attr]
                self.assertFalse(store.get(stopped.id).visible)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_runtime_reconciliation_preserves_recently_closed_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "dev", "bash", tmp)
                second = store.create("second", "dev", "bash", tmp)
                second_closed_at = "2026-08-12T19:00:00.000001+00:00"
                first_closed_at = "2026-08-12T19:00:00.000002+00:00"
                with patch(
                    "termdeck.session_lifecycle.utc_now",
                    return_value=second_closed_at,
                ):
                    hide_records(store, [second])
                with patch(
                    "termdeck.session_lifecycle.utc_now",
                    return_value=first_closed_at,
                ):
                    hide_records(store, [first])

                store.update_runtime_states(
                    [
                        (first.id, "running", False, 101),
                        (second.id, "running", False, 202),
                    ]
                )

                self.assertEqual(
                    [record.id for record in store.hidden_records()],
                    [first.id, second.id],
                )
                self.assertEqual(
                    [record.id for record in recently_closed_records(store)],
                    [first.id, second.id],
                )
                self.assertEqual(
                    store.get(first.id).last_opened_at,  # type: ignore[union-attr]
                    first_closed_at,
                )
                self.assertEqual(
                    store.get(second.id).last_opened_at,  # type: ignore[union-attr]
                    second_closed_at,
                )
            finally:
                store.close()

    def test_pinned_tabs_are_front_loaded_and_protected_until_unpinned(self):
        import tempfile
        from pathlib import Path
        from termdeck.session_lifecycle import hide_records, pin_records, recently_closed_records, reopen_closed_records, unpin_records
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("shell", "dev", "bash", tmp, window_id="main", window_title="Main", tab_index=0)
                second = store.create("agent", "ai", "codex", tmp, window_id="main", window_title="Main", tab_index=1)
                pinned = pin_records(store, [second])[0]
                self.assertTrue(pinned.pinned)
                visible = store.windows_with_visible_sessions()[0][2]
                self.assertEqual([row.name for row in visible], ["agent", "shell"])

                blocked = hide_records(store, [store.get(second.id)])[0]  # type: ignore[list-item]
                self.assertFalse(blocked.ok)
                self.assertTrue(store.get(second.id).visible)  # type: ignore[union-attr]

                unpin_records(store, [store.get(second.id)])  # type: ignore[list-item]
                hidden = hide_records(store, [store.get(second.id)])[0]  # type: ignore[list-item]
                self.assertTrue(hidden.ok)
                closed = recently_closed_records(store)
                self.assertEqual([row.name for row in closed], ["agent"])
                reopened = reopen_closed_records(store, closed)[0]
                self.assertTrue(reopened.visible)
                self.assertEqual(store.get(second.id).status, "new")  # type: ignore[union-attr]
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
