import sqlite3
import tempfile
import unittest
from pathlib import Path

from termdeck.models import SessionRecord
from termdeck.session_store import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_file_registry_uses_responsive_sqlite_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                journal_mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
                synchronous = store.conn.execute("PRAGMA synchronous").fetchone()[0]
                busy_timeout = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]

                self.assertEqual(str(journal_mode).casefold(), "wal")
                self.assertEqual(int(synchronous), 1)  # SQLite NORMAL
                self.assertEqual(int(busy_timeout), 1000)
            finally:
                store.close()

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
                self.assertIsNone(rows[0].recovery_command)
                self.assertIsNone(rows[0].recovery_cwd)
            finally:
                store.close()

    def test_window_captain_mapping_is_persistent_and_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                captain = store.create(
                    "captain",
                    "ai",
                    "codex",
                    tmp,
                    window_id="win-a",
                    window_title="A",
                )
                store.set_window_captain("win-a", captain.id)
                self.assertEqual(store.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
                self.assertFalse(store.clear_window_captain("win-a", session_id="td-not-captain"))
                self.assertEqual(store.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
            finally:
                store.close()

            reopened = SessionStore(db)
            try:
                self.assertEqual(reopened.captain_for_window("win-a").id, captain.id)  # type: ignore[union-attr]
                self.assertTrue(reopened.clear_window_captain("win-a", session_id=captain.id))
                self.assertIsNone(reopened.captain_for_window("win-a"))
            finally:
                reopened.close()

    def test_window_captain_validates_membership_and_cleans_stale_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "ai", "codex", tmp, window_id="win-a")
                second = store.create("second", "ai", "codex", tmp, window_id="win-a")
                foreign = store.create("foreign", "ai", "codex", tmp, window_id="win-b")

                with self.assertRaises(KeyError):
                    store.set_window_captain("win-a", "td-missing")
                with self.assertRaises(ValueError):
                    store.set_window_captain("win-a", foreign.id)

                store.set_window_captain("win-a", first.id)
                store.update(first.id, window_id="win-b")
                self.assertIsNone(store.captain_for_window("win-a"))
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM window_captains WHERE session_id = ?",
                        (first.id,),
                    ).fetchone()[0],
                    0,
                )

                store.set_window_captain("win-a", second.id)
                store.delete(second.id)
                self.assertIsNone(store.captain_for_window("win-a"))

                store.conn.execute(
                    "INSERT INTO window_captains (window_id, session_id) VALUES (?, ?)",
                    ("win-stale", "td-gone"),
                )
                store.conn.commit()
                self.assertIsNone(store.captain_for_window("win-stale"))
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM window_captains WHERE window_id = 'win-stale'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                store.close()

    def test_recovery_metadata_updates_are_batched_without_reordering_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "terminal", "/bin/bash", tmp)
                second = store.create("second", "terminal", "/bin/bash", tmp)
                second = store.update(
                    second.id,
                    recovery_command="codex --search",
                    recovery_cwd=str(Path(tmp) / "old"),
                    last_opened_at=second.last_opened_at,
                )
                first_opened_at = first.last_opened_at
                second_opened_at = second.last_opened_at

                recovery_cwd = str(Path(tmp) / "repo")
                count = store.update_recovery_metadata(
                    [
                        (first.id, "codex --yolo", recovery_cwd),
                        (second.id, None, str(Path(tmp) / "ignored")),
                    ]
                )

                self.assertEqual(count, 2)
                self.assertEqual(store.get(first.id).recovery_command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).recovery_cwd, recovery_cwd)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_cwd)  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).last_opened_at, first_opened_at)  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).last_opened_at, second_opened_at)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_exact_codex_uuid_is_not_downgraded_by_launcher_only_observation(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        exact = f"codex --yolo resume {session_id}"
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("agent", "ai", "/bin/bash", tmp)
                store.update(
                    record.id,
                    recovery_command=exact,
                    recovery_cwd=str(Path(tmp) / "exact"),
                )

                weaker_count = store.update_recovery_metadata(
                    [(record.id, "codex --yolo", str(Path(tmp) / "weaker"))]
                )
                preserved = store.get(record.id)

                self.assertEqual(weaker_count, 0)
                self.assertEqual(preserved.recovery_command, exact)  # type: ignore[union-attr]
                self.assertEqual(preserved.recovery_cwd, str(Path(tmp) / "exact"))  # type: ignore[union-attr]

                clear_count = store.update_recovery_metadata([(record.id, None, None)])
                cleared = store.get(record.id)
                self.assertEqual(clear_count, 1)
                self.assertIsNone(cleared.recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(cleared.recovery_cwd)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_runtime_reconciliation_updates_are_batched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "ai", "codex", tmp)
                second = store.create("second", "ai", "claude", tmp)

                count = store.update_runtime_states(
                    [
                        (first.id, "running", True, 101),
                        (second.id, "paused", False, 202),
                    ]
                )

                self.assertEqual(count, 2)
                refreshed_first = store.get(first.id)
                refreshed_second = store.get(second.id)
                self.assertEqual((refreshed_first.status, refreshed_first.visible, refreshed_first.last_pid), ("running", True, 101))
                self.assertEqual((refreshed_second.status, refreshed_second.visible, refreshed_second.last_pid), ("paused", False, 202))
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
                self.assertEqual(fallback, [])
            finally:
                store.close()

    def test_each_terminal_gets_a_unique_private_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                root / "sessions.sqlite3",
                auth_root=root / "terminal-auth",
                account_source=root / "empty-source",
            )
            try:
                first = store.create("first", "ai", "bash", tmp)
                second = store.create("second", "ai", "bash", tmp)

                self.assertNotEqual(first.auth_scope_id, second.auth_scope_id)
                self.assertNotEqual(first.codex_home, second.codex_home)
                for record in (first, second):
                    home = Path(record.codex_home)
                    self.assertTrue(home.is_dir())
                    self.assertTrue((home / "bin" / "codex-auth").is_file())
                    self.assertFalse((home / "auth.json").exists())
            finally:
                store.close()

    def test_direct_upsert_assigns_and_preserves_immutable_auth_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                root / "sessions.sqlite3",
                auth_root=root / "terminal-auth",
                account_source=root / "empty-source",
            )
            try:
                imported = SessionRecord(
                    id="td-adopted",
                    name="adopted",
                    tag="external",
                    command="bash",
                    cwd=tmp,
                    tmux_name="external-shell",
                    created_at="now",
                    last_opened_at="now",
                    external=True,
                )
                store.upsert(imported)
                saved = store.get(imported.id)
                self.assertIsNotNone(saved)
                self.assertTrue(saved.auth_scope_id)  # type: ignore[union-attr]
                self.assertTrue(Path(saved.codex_home).is_dir())  # type: ignore[arg-type,union-attr]

                replacement = SessionRecord(
                    **(
                        dict(saved.__dict__)  # type: ignore[union-attr]
                        | {
                            "auth_scope_id": "auth-forged",
                            "codex_home": str(root / "forged-home"),
                            "name": "updated",
                        }
                    )
                )
                store.upsert(replacement)
                preserved = store.get(imported.id)
                self.assertEqual(preserved.name, "updated")  # type: ignore[union-attr]
                self.assertEqual(preserved.auth_scope_id, saved.auth_scope_id)  # type: ignore[union-attr]
                self.assertEqual(preserved.codex_home, saved.codex_home)  # type: ignore[union-attr]
                with self.assertRaises(ValueError):
                    store.update(imported.id, codex_home=str(root / "other-home"))
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
                self.assertIsNone(record.recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(record.recovery_cwd)  # type: ignore[union-attr]
                self.assertTrue(record.auth_scope_id)  # type: ignore[union-attr]
                self.assertTrue(Path(record.codex_home).is_dir())  # type: ignore[arg-type,union-attr]
                self.assertIsNone(record.selected_account)  # type: ignore[union-attr]
                self.assertFalse(record.auth_restart_required)  # type: ignore[union-attr]
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
