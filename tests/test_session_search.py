import unittest

from termdeck.models import SessionRecord
from termdeck.session_search import render_search_table, search_sessions


def record(name="codex-one", tag="ai", command="codex", cwd="/tmp/project", session_id="td-search-001"):
    return SessionRecord(
        id=session_id,
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

    def test_typo_tolerant_metadata_search_matches_transposition(self):
        row = record(name="pytest", tag="tests", command="pytest")
        result = search_sessions([row], "pyetst")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.matches[0].name, "pytest")
        self.assertIn("name", result.matches[0].matched_fields)

    def test_typo_tolerant_metadata_search_supports_each_edit_operation(self):
        row = record(name="python", tag="dev", command="python")
        cases = {
            "insertion": "pythonn",
            "deletion": "pyton",
            "substitution": "pythom",
            "transposition": "pyhton",
        }
        for operation, query in cases.items():
            with self.subTest(operation=operation, query=query):
                result = search_sessions([row], query)
                self.assertEqual(result.count, 1)
                self.assertEqual(result.matches[0].name, "python")

    def test_typo_tolerant_multi_term_search_keeps_all_terms_required(self):
        rows = [
            record(name="pytest", tag="tests", command="pytest"),
            record(name="pytest", tag="dev", command="pytest"),
        ]
        result = search_sessions(rows, "pyetst tests")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.matches[0].tag, "tests")

    def test_literal_metadata_match_ranks_above_fuzzy_match(self):
        rows = [
            record(name="pytest", tag="tests", session_id="fuzzy"),
            record(name="pyetst-notes", tag="tests", session_id="literal"),
        ]
        result = search_sessions(rows, "pyetst")
        self.assertEqual([match.session_id for match in result.matches], ["literal", "fuzzy"])

    def test_short_terms_do_not_fuzzy_match_unrelated_metadata(self):
        row = record(name="app", tag="dev", command="app")
        result = search_sessions([row], "api")
        self.assertEqual(result.count, 0)

    def test_typo_tolerant_output_search_matches_recent_capture(self):
        row = record(name="build", tag="tests")
        result = search_sessions(
            [row],
            "compilaton failed",
            include_output=True,
            capture_resolver=lambda *_: "starting build\ncompilation failed\nfinished",
        )
        self.assertEqual(result.count, 1)
        self.assertEqual(result.matches[0].output_match_count, 1)
        self.assertEqual(result.matches[0].output_snippets[0].text, "compilation failed")

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
