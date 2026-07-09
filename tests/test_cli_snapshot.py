import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from termdeck.cli import cmd_restore_plan, cmd_snapshot_export, cmd_snapshot_import
from termdeck.session_store import SessionStore


class CliSnapshotTests(unittest.TestCase):
    def test_snapshot_export_restore_plan_and_import_are_json_cli_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            snapshot_path = Path(tmp) / "backup.snapshot.json"
            store = SessionStore(db_path)
            try:
                first = store.create("shell", "dev", "bash", tmp, window_id="win-dev", window_title="Dev", tab_index=0)
                second = store.create("agent", "ai", "codex", tmp, window_id="win-ai", window_title="Agents", tab_index=0)
                store.update(first.id, status="running")
                store.update(second.id, visible=False)
            finally:
                store.close()

            export_args = argparse.Namespace(db=db_path, name="cli-backup", tag=None, visible_only=False, running_only=False, all=False, output=snapshot_path)
            self.assertEqual(cmd_snapshot_export(export_args), 0)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["schema"], "termdeck.browser-snapshot")
            self.assertEqual(snapshot["counts"]["tabs"], 2)

            restore_args = argparse.Namespace(db=db_path, tag=None, all=False, limit=50)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(cmd_restore_plan(restore_args), 0)
            plan = json.loads(buffer.getvalue())
            self.assertEqual(plan["count"], 1)
            self.assertEqual(plan["windows"][0]["tabs"][0]["name"], "shell")

            import_args = argparse.Namespace(path=snapshot_path, db=Path(tmp) / "imported.sqlite3", replace=False, dry_run=True, visible_only=False, preserve_window_ids=False, restart_stopped=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(cmd_snapshot_import(import_args), 0)
            imported = json.loads(buffer.getvalue())
            self.assertEqual(imported["action"], "snapshot-import")
            self.assertTrue(imported["ok"])
            self.assertEqual(imported["count"], 2)
            self.assertTrue(all(row["planned"] for row in imported["sessions"]))


if __name__ == "__main__":
    unittest.main()
