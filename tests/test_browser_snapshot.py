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
    def test_snapshot_round_trip_preserves_automatic_and_same_basename_manual_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "source.sqlite3", provision_auth_scopes=False)
            restored = SessionStore(Path(tmp) / "restored.sqlite3", provision_auth_scopes=False)
            try:
                cwd = str(Path(tmp) / "arch")
                auto = store.create("arch", "automatic", "bash", cwd, automatic_name=True)
                manual = store.create("arch", "manual", "bash", cwd, automatic_name=False)
                store.update(auto.id, status="running", last_pid=123)
                store.update(manual.id, status="stopped", pinned=True)
                snapshot = json.loads(browser_snapshot_json(store.list(), name="names"))
                planned = import_browser_snapshot(restored, snapshot, dry_run=True)
                imported = import_browser_snapshot(restored, snapshot)
                self.assertEqual(
                    {row.tag: row.automatic_name for row in planned.rows},
                    {"automatic": True, "manual": False},
                )
                self.assertEqual(
                    {row["tag"]: row["automatic_name"] for row in imported.to_dict()["sessions"]},
                    {"automatic": True, "manual": False},
                )
                for row in imported.rows:
                    record = restored.get(row.session_id)
                    self.assertEqual(record.name, "arch")
                    self.assertEqual(record.cwd, cwd)
                    self.assertEqual(record.automatic_name, row.tag == "automatic")
                    self.assertIsNone(record.last_pid)
                    self.assertEqual(record.status, "running" if row.tag == "automatic" else "stopped")
            finally:
                store.close()
                restored.close()

    def test_legacy_snapshots_infer_names_and_reject_malformed_explicit_intent(self):
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                snapshot = {
                    "schema": "termdeck.browser-snapshot", "schema_version": version,
                    "windows": [{"title": "Names", "tabs": [
                        {"name": "arch", "command": "bash", "cwd": "/home/arch"},
                        {"name": "outreach", "command": "bash", "cwd": "/home/arch"},
                    ]}],
                }
                store = SessionStore(Path(tmp) / "sessions.sqlite3", provision_auth_scopes=False)
                try:
                    imported = import_browser_snapshot(store, snapshot)
                    self.assertEqual([store.get(row.session_id).automatic_name for row in imported.rows], [True, False])
                    for invalid in ("false", 0, [], {}):
                        with self.subTest(invalid=invalid):
                            snapshot["windows"][0]["tabs"][0]["automatic_name"] = invalid
                            with self.assertRaisesRegex(BrowserSnapshotError, "automatic_name must be true or false"):
                                import_browser_snapshot(store, snapshot, mode="replace")
                            self.assertEqual(len(store.list()), 2)
                finally:
                    store.close()

    def test_snapshot_exports_hidden_and_stopped_state_without_tmux_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                running = store.create("api", "dev", "python -m http.server", tmp, window_id="win-dev", window_title="Dev", tab_index=1)
                hidden = store.create("logs", "logs", "tail -f app.log", tmp, window_id="win-dev", window_title="Dev", tab_index=0, visible=False)
                stopped = store.create("old", "scratch", "bash", tmp, window_id="win-old", window_title="Old", tab_index=0)
                store.update(running.id, status="running", last_pid=123, selected_account="work")
                store.update(stopped.id, status="stopped")
                payload = json.loads(browser_snapshot_json(store.list(include_archived=True), name="backup"))
            finally:
                store.close()

            self.assertEqual(payload["schema"], "termdeck.browser-snapshot")
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["counts"]["windows"], 2)
            self.assertEqual(payload["counts"]["tabs"], 3)
            self.assertEqual(payload["counts"]["hidden_tabs"], 1)
            self.assertEqual(payload["counts"]["stopped_tabs"], 1)
            tabs = [tab for window in payload["windows"] for tab in window["tabs"]]
            self.assertEqual([tab["name"] for tab in tabs if tab["session_id"] == hidden.id], ["logs"])
            self.assertNotIn("tmux_name", tabs[0])
            self.assertEqual(next(tab for tab in tabs if tab["session_id"] == running.id)["selected_account"], "work")

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
                                    {"session_id": "td-api", "name": "api", "tag": "dev", "command": "python -m http.server", "cwd": tmp, "status": "running", "visible": True, "tab_index": 0, "selected_account": "work"},
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
                self.assertIsNone(imported[0].selected_account)
                self.assertEqual(imported[0].pending_account, "work")
                # Generic browser state stays tool-independent. A private
                # Codex home is created only when the SessionStore explicitly
                # enables account-scope provisioning.
                self.assertIsNone(existing.codex_home)
                self.assertIsNone(existing.auth_scope_id)
                self.assertTrue(all(row.codex_home is None for row in imported))
                self.assertTrue(all(row.auth_scope_id is None for row in imported))
            finally:
                store.close()

    def test_snapshot_import_never_marks_layout_only_rows_recoverable(self):
        snapshot = {
            "schema": "termdeck.browser-snapshot",
            "schema_version": 2,
            "name": "intent-check",
            "windows": [
                {
                    "window_id": "win",
                    "title": "Window",
                    "tabs": [
                        {
                            "session_id": "archived",
                            "name": "archived",
                            "tag": "old",
                            "command": "bash",
                            "cwd": ".",
                            "status": "archived",
                            "recovery_intent": "active",
                        },
                        {
                            "session_id": "new",
                            "name": "new",
                            "tag": "new",
                            "command": "bash",
                            "cwd": ".",
                            "status": "new",
                            "recovery_intent": "active",
                        },
                    ],
                }
            ],
        }

        parsed = parse_snapshot(snapshot)
        tabs = parsed["windows"][0]["tabs"]
        self.assertEqual([tab["recovery_intent"] for tab in tabs], ["inactive", "inactive"])

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                result = import_browser_snapshot(store, snapshot, preserve_window_ids=True)
                self.assertEqual([row.recovery_intent for row in result.rows], ["inactive", "inactive"])
                self.assertEqual(
                    [record.recovery_intent for record in store.list(include_archived=True)],
                    ["inactive", "inactive"],
                )
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
