import tempfile
import unittest
from pathlib import Path

from termdeck.session_store import SessionStore


class SessionStoreLayoutTests(unittest.TestCase):
    def test_window_grouping_uses_visible_non_stopped_records_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                api = store.create("api", "dev", "python -m http.server", tmp, window_id="win-a", window_title="Dev", tab_index=1)
                logs = store.create("logs", "logs", "tail -f app.log", tmp, window_id="win-a", window_title="Dev", tab_index=0)
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win-a", window_title="Dev", tab_index=2, visible=False)
                stopped = store.create("stopped", "dev", "bash", tmp, window_id="win-b", window_title="Ops", tab_index=0)
                store.update(stopped.id, status="stopped")

                groups = store.windows_with_visible_sessions()

                self.assertEqual(len(groups), 1)
                window_id, title, records = groups[0]
                self.assertEqual(window_id, "win-a")
                self.assertEqual(title, "Dev")
                self.assertEqual([record.id for record in records], [logs.id, api.id])
                self.assertNotIn(hidden.id, [record.id for record in records])
            finally:
                store.close()

    def test_restore_candidates_stay_scoped_to_the_requested_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=0)
                second = store.create("second", "dev", "bash", tmp, window_id="win-b", window_title="B", tab_index=0)
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win-c", window_title="C", tab_index=0, visible=False)
                store.update(first.id, status="running")
                store.update(second.id, status="running")
                store.update(hidden.id, status="running")

                self.assertEqual([r.id for r in store.restore_candidates(window_id="win-a")], [first.id])
                # An empty/unknown window must not pull in every other window's
                # visible tabs — that collapsed multi-window restore into one.
                self.assertEqual(store.restore_candidates(window_id="empty", limit=10), [])
                self.assertEqual([r.id for r in store.restore_candidates(window_id="win-b")], [second.id])
                self.assertNotIn(hidden.id, [r.id for r in store.restore_candidates(limit=10)])
            finally:
                store.close()

    def test_unbounded_restore_keeps_all_eighty_five_generic_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                created = []
                for index in range(85):
                    record = store.create(
                        f"generic-{index:02d}",
                        "generic",
                        f"generic-tui --resume durable-{index:02d}",
                        tmp,
                        window_id="win-generic",
                        window_title="Generic tools",
                        tab_index=index,
                    )
                    created.append(store.update(record.id, status="running"))

                restored = store.restore_candidates(window_id="win-generic")

                self.assertEqual([row.id for row in restored], [row.id for row in created])
                self.assertEqual(len(restored), 85)
                self.assertTrue(all(row.recovery_recipe for row in restored))
                self.assertTrue(all(row.recovery_recipe_sha256 for row in restored))
                self.assertTrue(all(row.codex_home is None for row in restored))
                self.assertTrue(all(row.auth_scope_id is None for row in restored))
                self.assertEqual(len(store.restore_candidates(limit=12, window_id="win-generic")), 12)
            finally:
                store.close()

    def test_remembered_window_survives_when_all_tabs_are_stopped_and_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                record = store.create(
                    "worker",
                    "dev",
                    "bash",
                    tmp,
                    window_id="win-durable",
                    window_title="Durable",
                )
                store.update(record.id, status="stopped", visible=False)
            finally:
                store.close()

            reopened = SessionStore(db)
            try:
                self.assertEqual(
                    reopened.windows_for_restore(),
                    [("win-durable", "Durable", [])],
                )
            finally:
                reopened.close()

    def test_legacy_window_backfill_ignores_all_hidden_historical_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                visible = store.create(
                    "visible",
                    "dev",
                    "bash",
                    tmp,
                    window_id="win-open",
                    window_title="Open",
                )
                hidden = store.create(
                    "hidden",
                    "dev",
                    "bash",
                    tmp,
                    window_id="win-history",
                    window_title="History",
                    visible=False,
                )
                store.update(visible.id, status="running")
                store.update(hidden.id, status="stopped", visible=False)
                store.conn.execute("DROP TABLE remembered_windows")
                store.conn.commit()
            finally:
                store.close()

            migrated = SessionStore(db)
            try:
                self.assertEqual(migrated.remembered_windows(), [("win-open", "Open")])
            finally:
                migrated.close()

    def test_window_rename_updates_sessions_and_independent_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create(
                    "worker",
                    "dev",
                    "bash",
                    tmp,
                    window_id="win-a",
                    window_title="Before",
                )

                self.assertEqual(store.rename_window("win-a", "After"), 1)

                self.assertEqual(store.get(record.id).window_title, "After")
                self.assertEqual(store.remembered_windows(), [("win-a", "After")])
            finally:
                store.close()

    def test_old_database_migrates_layout_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                record = store.create("legacy", "dev", "bash", tmp)
            finally:
                store.close()

            # Simulate an existing user DB by reopening and checking additive columns remain readable.
            reopened = SessionStore(db)
            try:
                loaded = reopened.get(record.id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.window_id, "main")  # type: ignore[union-attr]
                self.assertEqual(loaded.window_title, "Main")  # type: ignore[union-attr]
                self.assertTrue(loaded.visible)  # type: ignore[union-attr]
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
