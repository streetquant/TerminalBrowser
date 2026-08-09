import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from gi.repository import Gio

from termdeck import gtk_app
from termdeck.session_store import DEFAULT_DB_PATH


class GuiRunBoundaryTest(TestCase):
    def test_application_identity_is_stable_and_scoped_to_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.sqlite3"
            second = root / "second.sqlite3"
            first.touch()
            alias = root / "first-alias.sqlite3"
            alias.symlink_to(first)

            first_id = gtk_app.application_id_for_database(first)

            self.assertEqual(first_id, gtk_app.application_id_for_database(alias))
            self.assertNotEqual(first_id, gtk_app.application_id_for_database(second))
            self.assertRegex(first_id, r"^dev\.termdeck\.TermDeck\.db_[0-9a-f]{32}$")
            self.assertTrue(Gio.Application.id_is_valid(first_id))
            self.assertEqual(gtk_app.canonical_database_path(alias), first.resolve())
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                gtk_app.application_id_for_database(root)

        self.assertEqual(
            gtk_app.application_id_for_database(None),
            gtk_app.application_id_for_database(DEFAULT_DB_PATH),
        )
        self.assertEqual(gtk_app.application_id_for_database(None), "dev.termdeck.TermDeck")
        with self.assertRaisesRegex(ValueError, "file-backed"):
            gtk_app.application_id_for_database(":memory:")

    def test_run_hides_consumed_cli_options_from_gtk_application(self) -> None:
        application = mock.Mock()
        application.run.return_value = 23
        db_path = Path("/tmp/termdeck-test.sqlite3")
        workspace_path = Path("/tmp/termdeck-test.workspace.json")

        with mock.patch.object(gtk_app, "TermDeckApplication", return_value=application) as constructor:
            result = gtk_app.run(db_path=db_path, workspace_path=workspace_path)

        constructor.assert_called_once_with(db_path=db_path, workspace_path=workspace_path)
        application.run.assert_called_once_with([sys.argv[0]])
        self.assertEqual(result, 23)


if __name__ == "__main__":
    import unittest

    unittest.main()
