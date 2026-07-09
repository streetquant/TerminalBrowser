import tempfile
import unittest
from pathlib import Path

from termdeck.session_store import SessionStore
from termdeck.workspace_launcher import dry_run_workspace, open_workspace, open_workspace_file
from termdeck.workspace_recipe import parse_recipe


class FakeBackend:
    available = True

    def __init__(self):
        self.created = []

    def create_or_resume(self, record):
        self.created.append(record.id)

    def pane_pid(self, record):
        return 4242


class WorkspaceLauncherTests(unittest.TestCase):
    def _recipe(self, tmp):
        return parse_recipe(
            {
                "name": "demo-browser-workspace",
                "windows": [
                    {
                        "title": "Dev",
                        "tabs": [
                            {"name": "api", "tag": "dev", "command": "python3 -m http.server", "cwd": "."},
                            {"name": "tests", "tag": "tests", "command": "pytest", "cwd": ".", "visible": False},
                        ],
                    },
                    {
                        "title": "Agents",
                        "tabs": [
                            {"name": "codex", "tag": "ai", "command": "codex", "cwd": "."},
                        ],
                    },
                ],
            },
            base_dir=tmp,
        )

    def test_dry_run_workspace_plans_without_touching_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                result = dry_run_workspace(self._recipe(tmp), start=False)
                self.assertTrue(result.dry_run)
                self.assertFalse(result.start)
                self.assertEqual(result.count, 3)
                self.assertEqual(store.list(include_archived=True), [])
                self.assertEqual(result.rows[0].status, "planned-register")
                self.assertEqual(result.rows[0].window_title, "Dev")
                self.assertEqual(result.rows[2].window_title, "Agents")
            finally:
                store.close()

    def test_open_workspace_no_start_registers_browser_window_and_tab_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                result = open_workspace(self._recipe(tmp), store, start=False)
                self.assertFalse(result.dry_run)
                self.assertFalse(result.start)
                self.assertEqual(result.count, 3)
                self.assertTrue(all(row.created for row in result.rows))
                self.assertFalse(any(row.started for row in result.rows))

                rows = store.list(include_archived=True)
                self.assertEqual([row.window_title for row in rows], ["Agents", "Dev", "Dev"])
                dev_rows = sorted([row for row in rows if row.window_title == "Dev"], key=lambda row: row.tab_index)
                self.assertEqual([row.name for row in dev_rows], ["api", "tests"])
                self.assertFalse(dev_rows[1].visible)
                self.assertEqual({row.status for row in rows}, {"new"})
            finally:
                store.close()

    def test_open_workspace_reuses_equivalent_capsules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                recipe = self._recipe(tmp)
                first = open_workspace(recipe, store, start=False)
                second = open_workspace(recipe, store, start=False, reuse=True)
                self.assertEqual(store.list(include_archived=True).__len__(), 3)
                self.assertEqual([row.session_id for row in first.rows], [row.session_id for row in second.rows])
                self.assertTrue(all(row.reused for row in second.rows))
                self.assertFalse(any(row.created for row in second.rows))
            finally:
                store.close()

    def test_open_workspace_can_start_with_backend_and_record_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            backend = FakeBackend()
            try:
                result = open_workspace(self._recipe(tmp), store, backend=backend, start=True)
                self.assertEqual(result.count, 3)
                self.assertTrue(all(row.started for row in result.rows))
                self.assertTrue(all(row.status == "running" for row in result.rows))
                self.assertEqual({row.pid for row in result.rows}, {4242})
                self.assertEqual(len(backend.created), 3)
                self.assertEqual({row.last_pid for row in store.list(include_archived=True)}, {4242})
            finally:
                store.close()

    def test_open_workspace_file_loads_json_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe_path = Path(tmp) / "workspace.json"
            recipe_path.write_text(
                '{"name":"one","tabs":[{"name":"shell","command":"{shell}","cwd":"."}]}',
                encoding="utf-8",
            )
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                result = open_workspace_file(recipe_path, store, start=False)
                self.assertEqual(result.workspace, "one")
                self.assertEqual(result.count, 1)
                self.assertEqual(result.rows[0].window_title, "Main")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
