import unittest

from termdeck.capture_search import render_output_search_table, search_session_output
from termdeck.models import SessionRecord


class FakeBackend:
    def __init__(self, captures):
        self.captures = captures

    def capture(self, record, lines=500):
        return self.captures.get(record.name, "")


def record(name="codex-one", tag="ai"):
    return SessionRecord(
        id=f"td-{name}",
        name=name,
        tag=tag,
        command="codex" if tag == "ai" else "pytest",
        cwd="/tmp/project",
        tmux_name=f"termdeck_{tag}_{name}",
        created_at="2026-01-01T00:00:00Z",
        last_opened_at="2026-01-01T00:00:00Z",
        status="running",
        window_title="Agents" if tag == "ai" else "Tests",
    )


class CaptureSearchTests(unittest.TestCase):
    def test_searches_all_selected_output_and_redacts_display_snippets(self):
        records = [record("codex-one", "ai"), record("pytest", "tests")]
        backend = FakeBackend({
            "codex-one": "boot\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\nfinal status ready\n",
            "pytest": "collected 4 tests\nall tests passed\n",
        })
        summary = search_session_output(records, "status ready", backend=backend, context=1)
        self.assertEqual(summary.match_count, 1)
        self.assertEqual(summary.matched_sessions, 1)
        row = summary.results[0]
        self.assertEqual(row.name, "codex-one")
        self.assertTrue(row.redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(summary.as_dict()))
        self.assertIn("Authorization header", row.redaction_labels)
        self.assertEqual(row.matches[0].line_number, 3)

    def test_table_renderer_is_browser_side_pane_ready(self):
        summary = search_session_output([record()], "needle", backend=FakeBackend({"codex-one": "one\ntwo needle\nthree"}))
        table = render_output_search_table(summary)
        self.assertIn("Search output", table)
        self.assertIn("codex-one", table)
        self.assertIn("two needle", table)


if __name__ == "__main__":
    unittest.main()
