import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from gi.repository import Gio

from termdeck import gtk_app
from termdeck.session_store import DEFAULT_DB_PATH


class GuiRunBoundaryTest(TestCase):
    def test_application_exports_quit_preserving_sessions_action(self) -> None:
        source = inspect.getsource(gtk_app.TermDeckApplication.__init__)
        self.assertIn("quit-preserving-sessions", source)
        self.assertIn("Gio.SimpleAction", source)
        app = mock.Mock()
        gtk_app.TermDeckApplication._activate_quit_preserving_sessions(
            app, None, None
        )
        app.quit_preserving_sessions.assert_called_once_with()

    def test_quit_refuses_when_tmux_lifecycle_is_not_safe(self) -> None:
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = SimpleNamespace(
            ok=False, detail="user systemd is unavailable"
        )
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=True,
        ):
            self.assertFalse(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        app.quit.assert_not_called()

    def test_quit_revalidates_tmux_lifecycle_before_closing(self) -> None:
        isolation = SimpleNamespace(ok=True, detail="safe")
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = isolation
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=True,
        ):
            self.assertTrue(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        self.assertIs(app._tmux_server_isolation, isolation)
        app.quit.assert_called_once_with()

    def test_non_main_child_quit_can_preserve_sessions_without_user_systemd(self) -> None:
        backend = mock.Mock()
        backend.ensure_server_lifecycle_isolated.return_value = SimpleNamespace(
            ok=False, detail="user systemd is unavailable"
        )
        app = SimpleNamespace(
            windows=[],
            backend=backend,
            quit=mock.Mock(),
            _tmux_server_isolation=None,
        )

        with mock.patch(
            "termdeck.gtk_app.process_exit_requires_tmux_isolation",
            return_value=False,
        ):
            self.assertTrue(
                gtk_app.TermDeckApplication.quit_preserving_sessions(app)
            )
        app.quit.assert_called_once_with()

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
            self.assertRegex(first_id, r"^dev\.t1000\.T1000\.db_[0-9a-f]{32}$")
            self.assertTrue(Gio.Application.id_is_valid(first_id))
            self.assertEqual(gtk_app.canonical_database_path(alias), first.resolve())
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                gtk_app.application_id_for_database(root)

        self.assertEqual(
            gtk_app.application_id_for_database(None),
            gtk_app.application_id_for_database(DEFAULT_DB_PATH),
        )
        self.assertEqual(gtk_app.application_id_for_database(None), "dev.t1000.T1000")
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

    def test_primary_startup_migrates_only_a_missing_managed_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "runtime" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            store = mock.Mock()
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch.object(gtk_app, "DEFAULT_DB_PATH", target), mock.patch.object(
                gtk_app, "migrate_legacy_database"
            ) as migrate, mock.patch.object(
                gtk_app, "SessionStore", return_value=store
            ) as constructor, mock.patch.object(
                gtk_app, "consume_pending_recovery_intents"
            ) as consume:
                result = gtk_app.open_primary_session_store(target)

            self.assertIs(result, store)
            migrate.assert_called_once_with(destination=target)
            constructor.assert_called_once_with(target)
            consume.assert_called_once_with(store)

    def test_existing_managed_registry_never_rechecks_legacy_hdd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            managed_root = Path(tmp) / "runtime" / "t1000-managed"
            target = managed_root.parent / "termdeck" / "sessions.sqlite3"
            target.parent.mkdir(parents=True)
            target.touch()
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch.object(gtk_app, "DEFAULT_DB_PATH", target), mock.patch.object(
                gtk_app, "migrate_legacy_database"
            ) as migrate, mock.patch.object(
                gtk_app, "SessionStore"
            ) as constructor, mock.patch.object(
                gtk_app, "consume_pending_recovery_intents"
            ) as consume:
                gtk_app.open_primary_session_store(target)

            migrate.assert_not_called()
            constructor.assert_called_once_with(target)
            consume.assert_called_once()


if __name__ == "__main__":
    import unittest

    unittest.main()
