import unittest

from termdeck.models import SessionRecord
from termdeck.session_search import render_search_table, search_sessions


def record(name="codex-one", tag="ai", command="codex", cwd="/tmp/project"):
    return SessionRecord(
        id="td-search-001",
        name=name,
        tag=tag,
        command=command,
        cwd=cwd,
        tmux_name=f"termdeck_{tag}_{name}",
        created_at="2026-01-01T00:00:00Z",
        last_opened_at="2026-01-01T00:00:00Z",
        status="running",
        window_id="agents",
        window_title="Agents",
        tab_index=0,
    )


class SessionSearchTests(unittest.TestCase):
    def test_metadata_search_matches_all_terms_and_ranks_name_first(self):
        rows = [
            record(name="codex-one", tag="ai"),
            record(name="pytest", tag="tests", command="pytest", cwd="/tmp/codex-fixtures"),
        ]
        result = search_sessions(rows, "codex ai")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.matches[0].session_id, "td-search-001")
        self.assertIn("name", result.matches[0].matched_fields)
        self.assertIn("tag", result.matches[0].matched_fields)

    def test_output_search_redacts_before_serialising_snippets(self):
        row = record(name="agent", tag="ai")

        def capture(_record, _lines):
            return "booted\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\nfinal status ready"

        result = search_sessions([row], "status ready", include_output=True, capture_resolver=capture)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.matches[0].output_match_count, 1)
        self.assertEqual(result.matches[0].output_snippets[0].line_number, 3)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(result.as_dict()))
        self.assertTrue(result.matches[0].redacted)

    def test_render_search_table_mentions_output_snippets(self):
        row = record()
        result = search_sessions([row], "needle", include_output=True, capture_resolver=lambda *_: "one\ntwo needle\nthree")
        table = render_search_table(result)
        self.assertIn("td-search-001", table)
        self.assertIn("line 2", table)
        self.assertIn("two needle", table)


if __name__ == "__main__":
    unittest.main()
