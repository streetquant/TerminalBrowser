from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from termdeck.cli import (
    _prepare_managed_session_database,
    build_parser,
    cmd_recovery_intent,
)
from termdeck.recovery_intent import (
    RecoveryIntentError,
    consume_pending_recovery_intents,
    recovery_intent_paths,
    request_recovery_intent,
)
from termdeck.session_store import SessionStore


class RecoveryIntentTests(unittest.TestCase):
    def _source(self, directory: Path) -> tuple[Path, str]:
        source = directory / "legacy" / "sessions.sqlite3"
        source.parent.mkdir()
        store = SessionStore(source)
        try:
            record = store.create(
                "prompts",
                "terminal",
                "/bin/bash",
                "/8tb/bug-bounty/prompts",
            )
            store.update(
                record.id,
                status="stopped",
                recovery_command="codex --yolo resume 01a03ac1-632e-7d60-9265-5e02dbd9b4c0",
                recovery_cwd="/8tb/bug-bounty/prompts",
            )
            return source, record.id
        finally:
            store.close()

    def _environment(self, root: Path) -> dict[str, str]:
        return {"T1000_MANAGED_ROOT": str(root)}

    def test_request_is_read_only_and_does_not_store_launch_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source, session_id = self._source(directory)
            source_bytes = source.read_bytes()
            source_stat = source.stat()
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                request = request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=self._environment(root),
                )

            paths = recovery_intent_paths(environ=self._environment(root))
            sidecar = paths.intent_dir / f"{session_id}.json"
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["external"], 0)
            self.assertNotIn("command", payload)
            self.assertNotIn("recovery_command", payload)
            self.assertEqual(request.fingerprint, payload["fingerprint"])
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(source.stat().st_mtime_ns, source_stat.st_mtime_ns)
            self.assertEqual(paths.state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.intent_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.lock_path.stat().st_mode & 0o777, 0o600)

    def test_request_requires_explicit_managed_ssd_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source, session_id = self._source(directory)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ):
                with self.assertRaises(RecoveryIntentError):
                    request_recovery_intent(
                        session_id,
                        name="prompts",
                        cwd="/8tb/bug-bounty/prompts",
                        environ={},
                    )

    def test_consume_applies_only_after_migration_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source, session_id = self._source(directory)
            environment = self._environment(root)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
                paths = recovery_intent_paths(environ=environment)
                target = paths.state_dir / "sessions.sqlite3"
                shutil.copy2(source, target)
                store = SessionStore(target)
                try:
                    self.assertEqual(store.get(session_id).recovery_intent, "inactive")
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                    self.assertEqual([outcome.state for outcome in outcomes], ["applied"])
                    self.assertEqual(store.get(session_id).recovery_intent, "active")
                    self.assertFalse(
                        (paths.intent_dir / f"{session_id}.json").exists()
                    )
                    self.assertEqual(
                        consume_pending_recovery_intents(
                            store,
                            environ=environment,
                        ),
                        [],
                    )
                finally:
                    store.close()

    def test_consume_retains_sidecar_when_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source, session_id = self._source(directory)
            environment = self._environment(root)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
                paths = recovery_intent_paths(environ=environment)
                target = paths.state_dir / "sessions.sqlite3"
                shutil.copy2(source, target)
                store = SessionStore(target)
                try:
                    store.update(
                        session_id,
                        last_opened_at="changed",
                        recovery_intent="inactive",
                    )
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                    self.assertEqual([outcome.state for outcome in outcomes], ["rejected"])
                    self.assertIn("changed", outcomes[0].detail)
                    self.assertTrue(
                        (paths.intent_dir / f"{session_id}.json").exists()
                    )
                    self.assertNotEqual(store.get(session_id).recovery_intent, "active")
                finally:
                    store.close()

    def test_cli_command_is_headless_and_never_accepts_a_command_argument(self) -> None:
        from termdeck.recovery_intent import RecoveryIntentRequest

        request = RecoveryIntentRequest(
            session_id="td-test",
            name="prompts",
            cwd="/8tb/bug-bounty/prompts",
            request_id="00000000-0000-4000-8000-000000000000",
            created_at="2026-01-01T00:00:00+00:00",
            fingerprint="0" * 64,
        )
        with mock.patch(
            "termdeck.recovery_intent.request_recovery_intent",
            return_value=request,
        ) as queue, contextlib.redirect_stdout(io.StringIO()) as output:
            result = cmd_recovery_intent(
                Namespace(
                    session_id="td-test",
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                )
            )
        self.assertEqual(result, 0)
        queue.assert_called_once_with(
            "td-test",
            name="prompts",
            cwd="/8tb/bug-bounty/prompts",
        )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertNotIn("command", payload)

    def test_cli_prepare_does_not_redirect_intent_command_to_legacy_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime" / "t1000-managed"
            legacy = Path(temporary) / "legacy"
            legacy.mkdir()
            (legacy / "sessions.sqlite3").touch()
            args = build_parser().parse_args(
                [
                    "recovery-intent",
                    "td-test",
                    "--name",
                    "prompts",
                    "--cwd",
                    "/8tb/bug-bounty/prompts",
                ]
            )
            with mock.patch.dict(
                os.environ,
                {"T1000_MANAGED_ROOT": str(root)},
                clear=False,
            ), mock.patch(
                "termdeck.cli.legacy_state_dir", return_value=legacy
            ):
                _prepare_managed_session_database(args)
            self.assertIsNone(args.db)


if __name__ == "__main__":
    unittest.main()
