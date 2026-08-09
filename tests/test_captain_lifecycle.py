import tempfile
from pathlib import Path
from unittest import TestCase

from termdeck.session_lifecycle import archive_records, hide_records, move_records
from termdeck.session_store import SessionStore


class CaptainLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SessionStore(Path(self.tempdir.name) / "sessions.sqlite3")
        self.addCleanup(self.store.close)

    def captain(self):
        record = self.store.create(
            "captain",
            "terminal",
            "codex",
            self.tempdir.name,
            window_id="source",
            window_title="Source",
        )
        record = self.store.update(record.id, status="running")
        self.store.set_window_captain("source", record.id)
        return record

    def test_hide_clears_captain_mapping(self) -> None:
        record = self.captain()

        hide_records(self.store, [record])

        self.assertIsNone(self.store.captain_for_window("source"))

    def test_archive_clears_captain_mapping(self) -> None:
        record = self.captain()

        archive_records(self.store, [record])

        self.assertIsNone(self.store.captain_for_window("source"))

    def test_move_does_not_transfer_captainship(self) -> None:
        record = self.captain()

        move_records(
            self.store,
            [record],
            window_id="destination",
            window_title="Destination",
        )

        self.assertIsNone(self.store.captain_for_window("source"))
        self.assertIsNone(self.store.captain_for_window("destination"))


if __name__ == "__main__":
    import unittest

    unittest.main()
