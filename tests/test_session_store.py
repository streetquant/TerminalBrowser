import sqlite3
import tempfile
import unittest
from pathlib import Path

from termdeck.session_store import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_create_update_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("api", "dev", "python -m http.server", tmp)
                self.assertTrue(record.tmux_name.startswith("termdeck_dev_api"))
                updated = store.update(record.id, status="running", last_pid=123)
                self.assertEqual(updated.status, "running")
                self.assertEqual(updated.last_pid, 123)
                rows = store.list()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].id, record.id)
                self.assertEqual(store.tags(), ["dev"])
            finally:
                store.close()

    def test_window_restore_groups_visible_non_stopped_sessions_in_tab_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                second = store.create("second", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=1)
                first = store.create("first", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=0)
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win-a", window_title="A", tab_index=2)
                stopped = store.create("stopped", "dev", "bash", tmp, window_id="win-b", window_title="B", tab_index=0)
                third = store.create("third", "dev", "bash", tmp, window_id="win-b", window_title="B", tab_index=1)

                store.update(hidden.id, visible=False)
                store.update(stopped.id, status="stopped")

                groups = store.windows_with_visible_sessions()
                self.assertEqual(len(groups), 2)
                self.assertEqual(groups[0][0], "win-a")
                self.assertEqual([record.id for record in groups[0][2]], [first.id, second.id])
                self.assertEqual(groups[1][0], "win-b")
                self.assertEqual([record.id for record in groups[1][2]], [third.id])
            finally:
                store.close()

    def test_restore_candidates_prefers_visible_running_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                visible = store.create("visible", "dev", "bash", tmp, visible=True)
                hidden = store.create("hidden", "dev", "bash", tmp, visible=False)
                stopped = store.create("stopped", "dev", "bash", tmp, visible=True)
                store.update(stopped.id, status="stopped")

                restored = store.restore_candidates(limit=10)
                self.assertEqual([record.id for record in restored], [visible.id])

                store.update(visible.id, visible=False)
                fallback = store.restore_candidates(limit=10)
                self.assertIn(hidden.id, [record.id for record in fallback])
                self.assertNotIn(stopped.id, [record.id for record in fallback])
            finally:
                store.close()

    def test_schema_migrates_database_from_first_mvp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    tmux_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_pid INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sessions
                    (id, name, tag, command, cwd, tmux_name, created_at, last_opened_at, status, last_pid)
                VALUES
                    ('td-old', 'old', 'default', 'bash', ?, 'termdeck_default_old', 'now', 'now', 'running', 42)
                """,
                (tmp,),
            )
            conn.commit()
            conn.close()

            store = SessionStore(db)
            try:
                record = store.get("td-old")
                self.assertIsNotNone(record)
                self.assertEqual(record.window_id, "main")  # type: ignore[union-attr]
                self.assertEqual(record.window_title, "Main")  # type: ignore[union-attr]
                self.assertEqual(record.tab_index, 0)  # type: ignore[union-attr]
                self.assertTrue(record.visible)  # type: ignore[union-attr]
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
