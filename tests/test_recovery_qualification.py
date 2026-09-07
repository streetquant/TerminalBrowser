from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from termdeck.cli import cmd_recovery_qualification
from termdeck.recovery_qualification import qualify_recovery
from termdeck.session_store import SessionStore


class RecoveryQualificationTests(unittest.TestCase):
    _STORAGE = {
        "CODEX_STORAGE_FABRIC": "direct-root",
        "CODEX_WORKSPACE_STORAGE_CLASS": "non-rotational-block",
        "CODEX_STORAGE_ATTESTATION": "receipt-for-test-cell",
    }

    @staticmethod
    def _claim_table(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE recovery_claims (
                    target_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _create_record(
        path: Path,
        cwd: Path,
        *,
        command: str = "/bin/bash",
        status: str = "recovery-pending",
        recovery_intent: str | None = "active",
        restart_policy: str = "always",
        account_source: Path | None = None,
    ):
        store = SessionStore(path, account_source=account_source)
        try:
            record = store.create(
                "pane",
                "test",
                command,
                cwd,
                restart_policy=restart_policy,
            )
            changes: dict[str, object] = {"status": status}
            if recovery_intent is not None:
                changes["recovery_intent"] = recovery_intent
            return_record = store.update(record.id, **changes)
            return return_record
        finally:
            store.close()

    def test_qualification_is_read_only_and_accounts_for_every_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            eligible = self._create_record(db, root)
            stopped = self._create_record(
                db,
                root,
                status="stopped",
                recovery_intent="inactive",
            )
            deferred = self._create_record(db, root / "not-present")
            self._claim_table(db)

            before = hashlib.sha256(db.read_bytes()).hexdigest()
            with mock.patch.dict(os.environ, self._STORAGE, clear=False):
                report = qualify_recovery(db, now=1_700_000_000)

            self.assertTrue(report.ok)
            self.assertTrue(report.read_only)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
            self.assertEqual(len(report.panes), 3)
            self.assertEqual(report.counts, {
                "eligible": 1,
                "deferred": 1,
                "fail_closed": 0,
                "skipped": 1,
            })
            self.assertEqual(report.as_dict()["accounting"], {
                "input": 3,
                "classified": 3,
                "unaccounted": 0,
                "disjoint": True,
            })
            by_id = {row.session_id: row for row in report.panes}
            self.assertEqual(by_id[eligible.id].classification, "eligible")
            self.assertEqual(by_id[stopped.id].classification, "skipped")
            self.assertEqual(by_id[deferred.id].classification, "deferred")
            self.assertEqual(by_id[deferred.id].primary_reason.code, "cwd_unavailable")

    def test_each_required_gate_is_reported_for_native_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            account_source = root / "account-source"
            account_source.mkdir()
            thread_id = "01a03ac1-632e-7d60-9265-5e02dbd9b4c0"
            record = self._create_record(
                db,
                root,
                command="codex",
                account_source=account_source,
            )
            store = SessionStore(db, account_source=account_source)
            try:
                store.update(
                    record.id,
                    recovery_command=f"codex --yolo resume {thread_id}",
                    recovery_cwd=str(root),
                    status="recovery-pending",
                    recovery_intent="active",
                )
                record = store.get(record.id)
            finally:
                store.close()
            self._claim_table(db)

            with mock.patch.dict(os.environ, self._STORAGE, clear=False):
                report = qualify_recovery(
                    db,
                    rollout_states={thread_id: {"state": "exact", "detail": "fixture rollout"}},
                )

            self.assertTrue(report.ok)
            row = report.panes[0]
            self.assertEqual(row.classification, "eligible")
            self.assertEqual({name: check.state for name, check in row.checks.items()}, {
                "auth_scope": "pass",
                "codex_rollout": "pass",
                "coordinator_claimability": "pass",
                "cwd": "pass",
                "identity": "pass",
                "lifecycle": "pass",
                "recovery_intent": "pass",
                "recipe": "pass",
                "storage_attestation": "pass",
            })

    def test_claim_conflict_defers_without_creating_or_replacing_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            record = self._create_record(db, root)
            self._claim_table(db)
            target_key = "session:" + hashlib.sha256(
                f"session\0{record.id}".encode("utf-8")
            ).hexdigest()
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    "INSERT INTO recovery_claims VALUES (?, ?, ?, ?, ?)",
                    (target_key, "other-pane", "other-owner", "token", 2_000_000_000),
                )
                connection.commit()
            finally:
                connection.close()

            with mock.patch.dict(os.environ, self._STORAGE, clear=False):
                report = qualify_recovery(db, now=1_700_000_000)
            row = report.panes[0]
            self.assertEqual(row.classification, "deferred")
            self.assertEqual(row.primary_reason.code, "coordinator_claim_held")
            self.assertEqual(
                sqlite3.connect(db).execute("SELECT COUNT(*) FROM recovery_claims").fetchone()[0],
                1,
            )

    def test_duplicate_resume_targets_are_not_both_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            account_source = root / "account-source"
            account_source.mkdir()
            thread_id = "01a03ac1-632e-7d60-9265-5e02dbd9b4c0"
            first = self._create_record(db, root, command="codex", account_source=account_source)
            second = self._create_record(db, root, command="codex", account_source=account_source)
            store = SessionStore(db, account_source=account_source)
            try:
                for record in (first, second):
                    store.update(
                        record.id,
                        recovery_command=f"codex --yolo resume {thread_id}",
                        recovery_cwd=str(root),
                        status="recovery-pending",
                        recovery_intent="active",
                    )
                first_scope = store.get(first.id)
            finally:
                store.close()
            connection = sqlite3.connect(db)
            try:
                connection.execute("DROP INDEX idx_sessions_auth_scope")
                connection.execute("DROP INDEX idx_sessions_codex_home")
                connection.execute(
                    "UPDATE sessions SET auth_scope_id = ?, codex_home = ? WHERE id = ?",
                    (first_scope.auth_scope_id, first_scope.codex_home, second.id),
                )
                connection.commit()
            finally:
                connection.close()
            self._claim_table(db)

            with mock.patch.dict(os.environ, self._STORAGE, clear=False):
                report = qualify_recovery(
                    db,
                    rollout_states={thread_id: "exact"},
                )
            self.assertEqual(len(report.panes), 2)
            self.assertEqual(
                {row.classification for row in report.panes},
                {"deferred"},
            )
            self.assertTrue(
                all(
                    any(reason.code == "coordinator_target_conflict" for reason in row.reasons)
                    for row in report.panes
                )
            )

    def test_prompt_id_has_a_queued_intent_representation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            record = self._create_record(
                db,
                root,
                status="recovery-pending",
                recovery_intent="legacy",
            )
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    "UPDATE sessions SET id = ?, name = ? WHERE id = ?",
                    ("td-696ad6fa", "prompts", record.id),
                )
                connection.commit()
            finally:
                connection.close()
            self._claim_table(db)

            with mock.patch.dict(os.environ, self._STORAGE, clear=False):
                report = qualify_recovery(db)
            row = report.panes[0]
            self.assertEqual(row.session_id, "td-696ad6fa")
            self.assertEqual(row.classification, "deferred")
            self.assertEqual(row.primary_reason.code, "recovery_intent_missing")
            self.assertEqual(row.queued_intent, {
                "schema": "termdeck.recovery-intent.v1",
                "intent": "active",
                "queued": True,
                "read_only": True,
                "session_id": "td-696ad6fa",
                "name": "prompts",
                "cwd": str(root),
            })
            self.assertNotIn("command", row.queued_intent)

    def test_cli_emits_json_without_opening_tmux_or_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "sessions.sqlite3"
            self._create_record(db, root)
            self._claim_table(db)
            output = io.StringIO()
            args = Namespace(db=db, rollout_root=None, max_rollout_files=4096)
            with mock.patch.dict(os.environ, self._STORAGE, clear=False), contextlib.redirect_stdout(output):
                result = cmd_recovery_qualification(args)
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["action"], "recovery-qualification")
            self.assertTrue(payload["read_only"])
            self.assertFalse(payload["mutated"])
            self.assertEqual(payload["accounting"]["unaccounted"], 0)


if __name__ == "__main__":
    unittest.main()
