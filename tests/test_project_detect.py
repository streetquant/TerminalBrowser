import json
import tempfile
import unittest
from pathlib import Path

from termdeck.project_detect import detect_project, project_detection_json, project_recipe_json


class ProjectDetectTests(unittest.TestCase):
    def test_node_python_and_ai_tabs_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"dev":"vite","test":"vitest","lint":"eslint ."}}', encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "tests").mkdir()
            detection = detect_project(root, include_ai=True)
            payload = detection.to_dict()
            windows = payload["recipe"]["windows"]
            self.assertEqual(payload["name"], root.name)
            self.assertEqual(windows[0]["title"], "Development")
            names = [tab["name"] for window in windows for tab in window["tabs"]]
            self.assertIn("shell", names)
            self.assertIn("dev server", names)
            self.assertIn("tests", names)
            self.assertIn("lint", names)
            self.assertIn("python tests", names)
            self.assertIn("codex", names)
            self.assertIn("claude", names)
            self.assertTrue(json.loads(project_detection_json(detection))["notes"])
            self.assertEqual(json.loads(project_recipe_json(detection))["name"], detection.name)

    def test_generic_project_still_gets_shell_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            detection = detect_project(tmp)
            names = [tab.name for window in detection.recipe.windows for tab in window.tabs]
            self.assertEqual(names, ["shell"])
            self.assertEqual(detection.notes[0].kind, "generic")


if __name__ == "__main__":
    unittest.main()
