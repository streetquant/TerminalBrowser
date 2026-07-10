import sys
from pathlib import Path
from unittest import TestCase, mock

from termdeck import gtk_app


class GuiRunBoundaryTest(TestCase):
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
