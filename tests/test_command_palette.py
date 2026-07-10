import unittest

from termdeck.command_palette import build_palette_items, search_palette, serialise_palette_items
from termdeck.models import SessionRecord


class CommandPaletteTests(unittest.TestCase):
    def sample_record(self) -> SessionRecord:
        return SessionRecord(
            id="td-demo-001",
            name="braze-sql-p1",
            tag="bugbounty",
            command="codex",
            cwd="/tmp/braze",
            tmux_name="termdeck_bugbounty_braze_sql_p1",
            created_at="2026-01-01T00:00:00Z",
            last_opened_at="2026-01-01T00:00:00Z",
            status="running",
            window_title="Research",
        )

    def test_palette_contains_window_actions_and_sessions_without_templates(self):
        items = build_palette_items([self.sample_record()])
        ids = {item.id for item in items}
        self.assertIn("builtin:new-tab", ids)
        self.assertIn("builtin:rename-window", ids)
        self.assertIn("builtin:new-window", ids)
        self.assertIn("builtin:master", ids)
        self.assertIn("session:td-demo-001", ids)
        self.assertFalse(any(item.kind == "template" for item in items))

    def test_search_finds_existing_session_by_tag_or_name(self):
        items = build_palette_items([self.sample_record()])
        self.assertEqual(search_palette("braze", items)[0].id, "session:td-demo-001")
        self.assertEqual(search_palette("bugbounty", items)[0].id, "session:td-demo-001")

    def test_search_finds_resource_and_pause_actions(self):
        items = build_palette_items([])
        self.assertEqual(search_palette("cpu ram", items)[0].id, "builtin:master")
        self.assertEqual(search_palette("pause selected", items)[0].id, "builtin:pause-selected")

    def test_search_finds_lifecycle_actions(self):
        items = build_palette_items([])
        self.assertEqual(search_palette("hide selected", items)[0].id, "builtin:hide-selected")
        self.assertEqual(search_palette("archive selected", items)[0].id, "builtin:archive-selected")

    def test_search_finds_reopen_closed_action(self):
        rows = search_palette("reopen closed", build_palette_items([]))
        self.assertEqual(rows[0].id, "builtin:reopen-closed")
        self.assertEqual(rows[0].payload, "reopen_closed")
        self.assertIn("browser", rows[0].badge)

    def test_search_finds_window_overview(self):
        rows = search_palette("windows resource", build_palette_items([]))
        self.assertEqual(rows[0].id, "builtin:windows")
        self.assertEqual(rows[0].payload, "window_overview")


    def test_search_finds_activity_monitor(self):
        rows = search_palette("waiting input error", build_palette_items([]))
        self.assertEqual(rows[0].id, "builtin:activity")
        self.assertEqual(rows[0].payload, "activity")

    def test_search_finds_pause_hot_resource_action(self):
        items = build_palette_items([])
        self.assertEqual(search_palette("pause hot cpu", items)[0].id, "builtin:pause-hot")

    def test_search_finds_session_search_surface(self):
        rows = search_palette("search tabs output", build_palette_items([]))
        self.assertEqual(rows[0].id, "builtin:search-sessions")
        self.assertEqual(rows[0].payload, "search_sessions")

    def test_serialisation_is_json_ready(self):
        rows = serialise_palette_items(search_palette("codex", build_palette_items([self.sample_record()])))
        self.assertTrue(rows)
        self.assertIn("id", rows[0])
        self.assertIn("destructive", rows[0])


if __name__ == "__main__":
    unittest.main()
