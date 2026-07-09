import json
import tempfile
import unittest
from pathlib import Path

from termdeck.browser_snapshot import (
    BrowserSnapshotError,
    browser_snapshot_json,
    import_browser_snapshot,
    load_snapshot,
    parse_snapshot,
    restore_plan,
)
from termdeck.session_store import SessionStore


class BrowserSnapshotTests(unittest.TestCase):
    def test_snapshot_exports_hidden_and_stopped_state_without_tmux_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                running = store.create("api", "dev", "python -m http.server", tmp, window_id="win-dev", window_title="Dev", tab_index=1)
                hidden = store.create("logs", "logs", "tail -f app.log", tmp, window_id="win-dev", window_title="Dev", tab_index=0, visible=False)
                stopped = store.create("old", "scratch", "bash", tmp, window_id="win-old", window_title="Old", tab_index=0)
                store.update(running.id, status="running", last_pid=123)
                store.update(stopped.id, status="stopped")
                payload = json.loads(browser_snapshot_json(store.list(include_archived=True), name="backup"))
            finally:
                store.close()

            self.assertEqual(payload["schema"], "termdeck.browser-snapshot")
            self.assertEqual(payload["counts"]["windows"], 2)
            self.assertEqual(payload["counts"]["tabs"], 3)
            self.assertEqual(payload["counts"]["hidden_tabs"], 1)
            self.assertEqual(payload["counts"]["stopped_tabs"], 1)
            tabs = [tab for window in payload["windows"] for tab in window["tabs"]]
            self.assertEqual([tab["name"] for tab in tabs if tab["session_id"] == hidden.id], ["logs"])
            self.assertNotIn("tmux_name", tabs[0])

    def test_parse_rejects_wrong_schema(self):
        with self.assertRaises(BrowserSnapshotError):
            parse_snapshot({"schema": "termdeck.workspace", "schema_version": 1, "windows": []})

    def test_snapshot_import_dry_run_preserves_layout_without_writing(self):
        snapshot = {
            "schema": "termdeck.browser-snapshot",
            "schema_version": 1,
            "name": "backup",
            "windows": [
                {
                    "window_id": "win-dev",
                    "title": "Dev",
                    "tabs": [
                        {"session_id": "td-old", "name": "api", "tag": "dev", "command": "python -m http.server", "cwd": ".", "status": "running", "visible": True, "tab_index": 2},
                        {"session_id": "td-hidden", "name": "logs", "tag": "logs", "command": "tail -f app.log", "cwd": ".", "status": "paused", "visible": False, "tab_index": 1},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                result = import_browser_snapshot(store, snapshot, dry_run=True, preserve_window_ids=True)
                self.assertEqual(result.count, 2)
                self.assertEqual(result.rows[0].window_id, "win-dev")
                self.assertEqual(result.rows[0].tab_index, 2)
                self.assertEqual(result.rows[1].status, "paused")
                self.assertEqual(store.list(include_archived=True), [])
            finally:
                store.close()

    def test_snapshot_import_replace_archives_existing_and_creates_restore_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "schema": "termdeck.browser-snapshot",
                        "schema_version": 1,
                        "name": "backup",
                        "windows": [
                            {
                                "window_id": "win-dev",
                                "title": "Dev",
                                "tabs": [
                                    {"session_id": "td-api", "name": "api", "tag": "dev", "command": "python -m http.server", "cwd": tmp, "status": "running", "visible": True, "tab_index": 0},
                                    {"session_id": "td-old", "name": "old", "tag": "scratch", "command": "bash", "cwd": tmp, "status": "stopped", "visible": True, "tab_index": 1},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                existing = store.create("existing", "dev", "bash", tmp)
                snapshot = load_snapshot(snapshot_path)
                result = import_browser_snapshot(store, snapshot, mode="replace", restart_stopped=True)
                self.assertEqual(result.count, 2)
                all_rows = store.list(include_archived=True)
                existing_row = store.get(existing.id)
                self.assertIsNotNone(existing_row)
                self.assertEqual(existing_row.status, "archived")  # type: ignore[union-attr]
                imported = [row for row in all_rows if row.id != existing.id]
                self.assertEqual([row.name for row in imported], ["api", "old"])
                self.assertEqual([row.status for row in imported], ["running", "new"])
                self.assertTrue(all(row.last_pid is None for row in imported))
            finally:
                store.close()

    def test_restore_plan_includes_visible_non_stopped_tabs_and_explains_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                visible = store.create("api", "dev", "python -m http.server", tmp, window_id="win-dev", window_title="Dev", tab_index=0)
                hidden = store.create("hidden", "dev", "bash", tmp, window_id="win-dev", window_title="Dev", tab_index=1, visible=False)
                stopped = store.create("stopped", "dev", "bash", tmp, window_id="win-dev", window_title="Dev", tab_index=2)
                store.update(stopped.id, status="stopped")
                plan = restore_plan(store.list(include_archived=True))
            finally:
                store.close()

            self.assertEqual(plan["count"], 1)
            self.assertEqual(plan["windows"][0]["tabs"][0]["session_id"], visible.id)
            skipped = {row["session_id"]: row["reason"] for row in plan["skipped"]}
            self.assertEqual(skipped[hidden.id], "hidden")
            self.assertEqual(skipped[stopped.id], "stopped or archived")


if __name__ == "__main__":
    unittest.main()
