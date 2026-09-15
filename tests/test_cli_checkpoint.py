import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.checkpointing import CheckpointResult
from termdeck.cli import (
    build_parser,
    cmd_checkpoint,
    cmd_install_checkpoint_service,
    cmd_uninstall_checkpoint_service,
)
from termdeck.persistence import PersistenceResult
from termdeck.session_store import SessionStore
from termdeck.shutdown_service import ShutdownServiceResult


class CheckpointCliTests(unittest.TestCase):
    def test_checkpoint_service_install_requires_system_mode_before_dispatch(self):
        args = argparse.Namespace(
            system=False,
            executable=Path("/tmp/t1000"),
            unit_dir=Path("/tmp/units"),
            user="max",
            uid=1000,
            home=Path("/home/arch"),
            required_mount=[],
            no_enable=False,
        )
        output = io.StringIO()
        with mock.patch("termdeck.cli.install_system_checkpoint_service") as install, contextlib.redirect_stdout(output):
            code = cmd_install_checkpoint_service(args)

        self.assertEqual(code, 1)
        install.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "install-checkpoint-service")
        self.assertIn("system-only", payload["error"])
        self.assertIn("--system", payload["error"])
        self.assertIn("--user max --uid 1000 --home /home/arch", payload["error"])

    def test_checkpoint_service_install_dispatches_only_the_system_contract(self):
        args = argparse.Namespace(
            system=True,
            executable=Path("/tmp/t1000"),
            unit_dir=Path("/tmp/units"),
            user="max",
            uid=1000,
            home=Path("/home/arch"),
            required_mount=[Path("/warp/projects"), Path("/warp/state")],
            no_enable=False,
        )
        expected = ShutdownServiceResult(
            "install-system",
            Path("/tmp/units/termdeck-checkpoint.service"),
            True,
            "installed",
        )
        output = io.StringIO()
        with mock.patch(
            "termdeck.cli.install_system_checkpoint_service",
            return_value=expected,
        ) as install, contextlib.redirect_stdout(output):
            code = cmd_install_checkpoint_service(args)

        self.assertEqual(code, 0)
        install.assert_called_once_with(
            executable=Path("/tmp/t1000"),
            user="max",
            uid=1000,
            home=Path("/home/arch"),
            required_mounts=(Path("/warp/projects"), Path("/warp/state")),
            unit_dir=Path("/tmp/units"),
            enable=True,
        )
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_checkpoint_service_removal_requires_system_mode_before_dispatch(self):
        args = argparse.Namespace(system=False, unit_dir=Path("/tmp/units"), no_disable=False)
        output = io.StringIO()
        with mock.patch("termdeck.cli.uninstall_system_checkpoint_service") as uninstall, contextlib.redirect_stdout(output):
            code = cmd_uninstall_checkpoint_service(args)

        self.assertEqual(code, 1)
        uninstall.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "uninstall-checkpoint-service")
        self.assertIn("system-only", payload["error"])
        self.assertIn("--system", payload["error"])

    def test_checkpoint_service_help_documents_the_system_only_contract(self):
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        install_help = subparsers.choices["install-checkpoint-service"].format_help()
        uninstall_help = subparsers.choices["uninstall-checkpoint-service"].format_help()

        self.assertIn("strict system shutdown checkpoint service", install_help)
        self.assertIn("required: install the strict system-level", install_help)
        self.assertIn("required exact service user: max", install_help)
        self.assertIn("required exact service uid: 1000", install_help)
        self.assertIn("required exact service home: /home/arch", install_help)
        self.assertNotIn("user shutdown checkpoint hook", install_help)
        self.assertIn("strict system shutdown checkpoint service", uninstall_help)
        self.assertIn("required: remove the strict system-level", uninstall_help)
        self.assertNotIn("user shutdown checkpoint hook", uninstall_help)

    def test_checkpoint_all_selects_only_active_managed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                running = store.create("running", "dev", "python worker.py", tmp)
                stopped = store.create("stopped", "dev", "python old.py", tmp)
                external = store.create("external", "dev", "python outside.py", tmp)
                start_error = store.create("broken", "dev", "python broken.py", tmp)
                recovery_pending = store.create("pending", "dev", "python pending.py", tmp)
                store.update(running.id, status="running")
                store.update(stopped.id, status="stopped")
                store.update(external.id, status="running", external=True)
                store.update(start_error.id, status="start-error")
                store.update(recovery_pending.id, status="recovery-pending")
            finally:
                store.close()

            expected = PersistenceResult(
                0,
                (
                    CheckpointResult(
                        running.id,
                        "checkpoint",
                        True,
                        True,
                        "saved",
                        1,
                    ),
                ),
            )
            args = argparse.Namespace(
                db=db,
                selectors=[],
                tag=None,
                all=True,
            )
            output = io.StringIO()
            backend = object()
            with mock.patch("termdeck.cli.TmuxBackend", return_value=backend), mock.patch(
                "termdeck.cli.persist_live_sessions",
                return_value=expected,
            ) as persist, contextlib.redirect_stdout(output):
                code = cmd_checkpoint(args)

            self.assertEqual(code, 0)
            called_store, called_backend = persist.call_args.args[:2]
            called_records = persist.call_args.kwargs["records"]
            self.assertIs(called_backend, backend)
            self.assertEqual([record.id for record in called_records], [running.id])
            self.assertTrue(called_store.path.samefile(db))
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["exact_count"], 1)
            self.assertEqual(payload["failed_count"], 0)

    def test_checkpoint_returns_success_for_expected_unsupported_process_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                record = store.create("worker", "dev", "python worker.py", tmp)
                store.update(record.id, status="running")
            finally:
                store.close()

            result = PersistenceResult(
                0,
                (
                    CheckpointResult(
                        record.id,
                        "unsupported",
                        False,
                        False,
                        "application-level checkpoint required",
                    ),
                ),
            )
            args = argparse.Namespace(db=db, selectors=[], tag=None, all=True)
            output = io.StringIO()
            with mock.patch("termdeck.cli.TmuxBackend"), mock.patch(
                "termdeck.cli.persist_live_sessions",
                return_value=result,
            ), contextlib.redirect_stdout(output):
                code = cmd_checkpoint(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["unsupported_count"], 1)
            self.assertEqual(payload["failed_count"], 0)

    def test_checkpoint_returns_failure_when_any_session_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db)
            try:
                record = store.create("worker", "dev", "python worker.py", tmp)
                store.update(record.id, status="running")
            finally:
                store.close()

            result = PersistenceResult(
                0,
                (
                    CheckpointResult(
                        record.id,
                        "checkpoint",
                        False,
                        False,
                        "DMTCP unavailable",
                    ),
                ),
            )
            args = argparse.Namespace(db=db, selectors=[], tag=None, all=True)
            with mock.patch("termdeck.cli.TmuxBackend"), mock.patch(
                "termdeck.cli.persist_live_sessions",
                return_value=result,
            ), contextlib.redirect_stdout(io.StringIO()):
                code = cmd_checkpoint(args)

            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
