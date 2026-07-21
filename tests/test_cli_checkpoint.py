import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.checkpointing import CheckpointResult
from termdeck.cli import cmd_checkpoint
from termdeck.persistence import PersistenceResult
from termdeck.session_store import SessionStore


class CheckpointCliTests(unittest.TestCase):
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
