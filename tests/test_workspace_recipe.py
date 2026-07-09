from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from termdeck.cli import cmd_workspace_plan
from termdeck.workspace_recipe import WorkspaceRecipeError, load_recipe, parse_recipe, recipe_from_sessions, recipe_summary_rows


class WorkspaceRecipeTests(unittest.TestCase):
    def test_top_level_tabs_are_normalised_to_main_window(self) -> None:
        recipe = parse_recipe(
            {
                "name": "demo",
                "tabs": [
                    {"name": "api", "tag": "dev", "command": "python3 -m http.server", "cwd": "app"},
                    {"name": "agent", "tag": "ai", "command": "codex", "visible": False},
                ],
            },
            base_dir="/tmp/project",
        )
        self.assertEqual(recipe.name, "demo")
        self.assertEqual(len(recipe.windows), 1)
        self.assertEqual(recipe.windows[0].title, "Main")
        self.assertEqual(recipe.windows[0].tabs[0].cwd, "/tmp/project/app")
        self.assertFalse(recipe.windows[0].tabs[1].visible)


    def test_recipe_from_sessions_exports_window_and_tab_order(self) -> None:
        from termdeck.models import SessionRecord

        def record(session_id: str, name: str, window: str, tab_index: int, visible: bool = True) -> SessionRecord:
            return SessionRecord(
                id=session_id,
                name=name,
                tag="dev",
                command=f"echo {name}",
                cwd="/work/app",
                tmux_name=f"tmux_{session_id}",
                created_at="2026-07-08T00:00:00+00:00",
                last_opened_at="2026-07-08T00:00:00+00:00",
                status="running",
                window_id=window.casefold(),
                window_title=window,
                tab_index=tab_index,
                visible=visible,
            )

        recipe = recipe_from_sessions(
            [record("td-2", "tests", "Dev", 1), record("td-1", "api", "Dev", 0), record("td-3", "agent", "Agents", 0, visible=False)],
            name="exported",
        )
        self.assertEqual(recipe.name, "exported")
        self.assertEqual([window.title for window in recipe.windows], ["Dev", "Agents"])
        self.assertEqual([tab.name for tab in recipe.windows[0].tabs], ["api", "tests"])
        self.assertFalse(recipe.windows[1].tabs[0].visible)

    def test_windows_and_summary_rows_are_browser_ordered(self) -> None:
        recipe = parse_recipe(
            {
                "name": "full-stack",
                "windows": [
                    {"title": "Dev", "tabs": [{"name": "server", "command": "npm run dev"}]},
                    {"title": "Tests", "tabs": [{"name": "pytest", "tag": "tests", "command": "pytest"}]},
                ],
            },
            base_dir="/work/app",
        )
        rows = recipe_summary_rows(recipe)
        self.assertEqual([row["window"] for row in rows], ["Dev", "Tests"])
        self.assertEqual(rows[0]["session_hint"], "full-stack-01-01")
        self.assertEqual(rows[1]["tag"], "tests")

    def test_missing_command_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceRecipeError):
            parse_recipe({"tabs": [{"name": "bad"}]}, base_dir="/tmp")

    def test_cli_workspace_plan_validates_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "termdeck.workspace.json"
            path.write_text(json.dumps({"name": "demo", "tabs": [{"name": "shell", "command": "{shell}"}]}), encoding="utf-8")
            args = argparse.Namespace(sample=False, path=path)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_workspace_plan(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload[0]["workspace"], "demo")
            self.assertEqual(payload[0]["window"], "Main")

    def test_load_recipe_reports_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(WorkspaceRecipeError):
                load_recipe(path)


if __name__ == "__main__":
    unittest.main()
