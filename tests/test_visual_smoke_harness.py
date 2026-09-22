from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_ui_smoke.py"
SPEC = importlib.util.spec_from_file_location("termdeck_render_ui_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class VisualSmokeHarnessTests(unittest.TestCase):
    def test_identify_size_parser_extracts_frame_dimensions(self) -> None:
        self.assertEqual(HARNESS._parse_identify_size("PNG 640x520 11930.1"), (640, 520))

    def test_window_dimensions_must_be_provided_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "must be provided together"):
                HARNESS.render(Path(tmp) / "frame.png", window_width=640)

    def test_gtk_minimum_size_expansion_is_reported_without_failing_capture(self) -> None:
        errors, warnings = HARNESS._classify_window_size((640, 480), (693, 480))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("minimum window geometry", warnings[0])

    def test_capture_smaller_than_requested_remains_a_failure(self) -> None:
        errors, warnings = HARNESS._classify_window_size((640, 480), (639, 480))
        self.assertEqual(warnings, [])
        self.assertEqual(errors, ["requested 640x480, captured 639x480"])

    def test_compact_capture_uses_synchronous_x_window_resize(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"windowsize", "--sync"', source)
        self.assertIn('"requested_window_size"', source)
        self.assertIn('"layout_error_lines"', source)
        self.assertIn('"layout_warning_lines"', source)
        self.assertIn('"XDG_RUNTIME_DIR": str(runtime_dir)', source)
        self.assertIn("_wait_for_smoke_panes(tmux, env)", source)
        self.assertIn('parser.add_argument("--window-width"', source)
        self.assertIn('parser.add_argument("--window-height"', source)


if __name__ == "__main__":
    unittest.main()
