import json
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import termdeck.auth_scope_rebind as auth_scope_rebind_module
from termdeck.auth_scope_rebind import (
    AuthScopeRebindClaim,
    AuthScopeRebindProof,
    rebind_auth_scope,
)
from termdeck.codex_auth import read_auth_state
from termdeck.models import SessionRecord
from termdeck.session_store import SessionStore, default_auth_root


RESUME_ID = "019f0000-0000-7000-8000-000000000001"


def _write_complete_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE _sqlx_migrations (
            version BIGINT PRIMARY KEY,
            description TEXT NOT NULL,
            installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            success BOOLEAN NOT NULL,
            checksum BLOB NOT NULL,
            execution_time BIGINT NOT NULL
        );
        CREATE TABLE backfill_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            last_watermark TEXT,
            last_success_at INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE thread_sections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            cwd TEXT NOT NULL,
            thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL
        );
        CREATE TABLE thread_dynamic_tools (
            thread_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY(thread_id, position)
        );
        CREATE TABLE thread_spawn_edges (id TEXT PRIMARY KEY);
        CREATE TABLE remote_control_enrollments (id TEXT PRIMARY KEY);
        CREATE TABLE external_agent_config_imports (id TEXT PRIMARY KEY);
        """
    )
    connection.executemany(
        """
        INSERT INTO _sqlx_migrations(
            version, description, success, checksum, execution_time
        ) VALUES (?, ?, 1, ?, ?)
        """,
        [
            (version, f"migration {version}", bytes([version]) * 48, version * 10)
            for version in range(1, 47)
        ],
    )
    connection.execute(
        """
        INSERT INTO backfill_state(
            id, status, last_watermark, last_success_at, updated_at
        ) VALUES (1, 'complete', 'sessions/last.jsonl', 1234, 1234)
        """
    )
    connection.execute(
        "INSERT INTO thread_sections(id, name) VALUES (?, 'Pinned')",
        ("01984de2-8f74-7c91-a3b2-5c5e937cf318",),
    )
    connection.commit()
    connection.close()
    path.chmod(0o600)


def _write_native_home(home: Path, selected: str = "beta") -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    accounts = home / "accounts"
    accounts.mkdir(mode=0o700)
    accounts.chmod(0o700)
    for name in ("alpha", "beta"):
        account = accounts / f"{name}.json"
        account.write_text(
            json.dumps({"account": name, "refresh_token": f"{name}-secret"}),
            encoding="utf-8",
        )
        account.chmod(0o600)
    (home / "auth.json").symlink_to(Path("accounts") / f"{selected}.json")
    (home / "current").write_text(f"{selected}\n", encoding="utf-8")
    (home / "current").chmod(0o600)


def _snapshot_tree(root: Path) -> dict[str, tuple[bool, str | bytes, int]]:
    snapshot: dict[str, tuple[bool, str | bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            value: str | bytes = os.readlink(path)
            snapshot[relative] = (True, value, mode)
        elif path.is_file():
            snapshot[relative] = (False, path.read_bytes(), mode)
        else:
            snapshot[relative] = (False, "", mode)
    return snapshot


class AuthScopeRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ssd = self.root / "ssd"
        self.ssd.mkdir(mode=0o700)
        self.ssd.chmod(0o700)
        self.managed = self.ssd / "managed"
        self.managed.mkdir(mode=0o700)
        self.managed.chmod(0o700)
        self.canonical = self.ssd / "canonical.codex"
        _write_native_home(self.canonical)
        (self.canonical / "sessions").mkdir(mode=0o700)
        (self.canonical / "archived_sessions").mkdir(mode=0o700)
        (self.canonical / "prompts").mkdir(mode=0o700)
        (self.canonical / "prompts" / "rules.md").write_text(
            "canonical prompt\n", encoding="utf-8"
        )
        _write_complete_state_db(self.canonical / "state_5.sqlite")
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        self.env = {"T1000_MANAGED_ROOT": str(self.managed)}
        self.store = SessionStore(
            self.root / "registry.sqlite3",
            provision_auth_scopes=False,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _record(
        self,
        *,
        session_id: str = "td-rebind",
        scope_id: str = "auth-rebind",
        old_home: Path | None = None,
        selected_account: str | None = "beta",
        command: str = "codex --yolo",
        recovery_command: str | None = None,
        external: bool = False,
    ) -> SessionRecord:
        old_home = old_home or (self.root / f"old-{session_id}")
        _write_native_home(old_home, selected=selected_account or "beta")
        record = self.store.create(
            session_id,
            "codex",
            command,
            str(self.project),
        )
        recovery_command = recovery_command or f"codex --yolo resume {RESUME_ID}"
        self.store.conn.execute(
            """
            UPDATE sessions
            SET status = 'missing', external = ?, recovery_command = ?,
                recovery_cwd = ?, auth_scope_id = ?, codex_home = ?,
                selected_account = ?, recovery_intent = 'active'
            WHERE id = ?
            """,
            (
                1 if external else 0,
                recovery_command,
                str(self.project),
                scope_id,
                str(old_home),
                selected_account,
                record.id,
            ),
        )
        self.store.conn.commit()
        refreshed = self.store.get(record.id)
        assert refreshed is not None
        return refreshed

    def _claim(self, record: SessionRecord, token: str = "claim-rebind") -> AuthScopeRebindClaim:
        keys = (f"session:{record.id}",)
        self.store.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_claims (
                target_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                claim_token TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        self.store.conn.execute(
            """
            INSERT INTO recovery_claims(
                target_key, session_id, owner_id, claim_token, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (keys[0], record.id, "worker-a", token, 4_000_000_000.0),
        )
        self.store.conn.commit()
        return AuthScopeRebindClaim(
            claim_token=token,
            owner_id="worker-a",
            target_keys=keys,
            session_id=record.id,
        )

    def _rebind(
        self,
        record: SessionRecord,
        *,
        proof: AuthScopeRebindProof | None = None,
        selected_account: str | None = None,
    ):
        if selected_account is not None:
            self.store.conn.execute(
                "UPDATE sessions SET selected_account = ? WHERE id = ?",
                (selected_account, record.id),
            )
            self.store.conn.commit()
            record = replace(record, selected_account=selected_account)
        claim = self._claim(record)
        return rebind_auth_scope(
            record,
            self.store,
            proof=proof or AuthScopeRebindProof(True, True, True),
            claim=claim,
            canonical_home=self.canonical,
            environ=self.env,
            now=100.0,
        )

    def test_rebind_preserves_exact_account_and_shared_sparse_state(self):
        record = self._record()
        old_snapshot = _snapshot_tree(Path(record.codex_home))
        result = self._rebind(record)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.selected_account_key, "beta")
        destination = Path(result.new_home or "")
        self.assertEqual(destination, default_auth_root(environ=self.env) / record.auth_scope_id)
        self.assertEqual(self.store.get(record.id).codex_home, str(destination))
        self.assertTrue((destination / "state_5.sqlite").is_file())
        self.assertEqual((destination / "state_5.sqlite").stat().st_nlink, 1)
        self.assertFalse(os.path.samefile(destination / "state_5.sqlite", self.canonical / "state_5.sqlite"))
        self.assertTrue((destination / "accounts").is_symlink())
        self.assertTrue(os.path.samefile(destination / "accounts", self.canonical / "accounts"))
        self.assertTrue((destination / "sessions").is_symlink())
        self.assertTrue(os.path.samefile(destination / "sessions", self.canonical / "sessions"))
        self.assertTrue((destination / "archived_sessions").is_symlink())
        self.assertTrue(os.path.samefile(destination / "archived_sessions", self.canonical / "archived_sessions"))
        self.assertTrue((destination / "prompts").is_symlink())
        self.assertTrue(os.path.samefile(destination / "prompts", self.canonical / "prompts"))
        state = read_auth_state(destination, account_source=self.canonical)
        self.assertTrue(state.catalog_shared)
        self.assertTrue(state.canonical_binding_ready)
        self.assertEqual(state.selected_account_key, "beta")
        self.assertEqual(_snapshot_tree(Path(record.codex_home)), old_snapshot)
        receipt = Path(result.receipt_path or "")
        self.assertTrue(receipt.is_file())
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["selected_account_key"], "beta")

    def test_rebind_never_copies_or_deletes_old_scope(self):
        record = self._record()
        old_home = Path(record.codex_home)
        marker = old_home / "must-remain.txt"
        marker.write_text("old scope evidence\n", encoding="utf-8")
        marker.chmod(0o600)
        before = _snapshot_tree(old_home)

        result = self._rebind(record)

        self.assertTrue(result.ok, result.detail)
        self.assertTrue(old_home.is_dir())
        self.assertEqual(_snapshot_tree(old_home), before)
        self.assertFalse((Path(result.new_home or "") / "must-remain.txt").exists())
        self.assertNotEqual(Path(result.new_home or "").parent, old_home.parent)

    def test_rebind_writes_receipt_and_registry_only_on_managed_ssd(self):
        record = self._record()
        old_home = Path(record.codex_home)
        before = _snapshot_tree(old_home)
        result = self._rebind(record)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(_snapshot_tree(old_home), before)
        self.assertTrue(Path(result.receipt_path or "").is_relative_to(self.ssd))
        self.assertTrue(Path(result.new_home or "").is_relative_to(self.ssd))
        self.assertFalse(Path(result.receipt_path or "").is_relative_to(old_home))

    def test_independent_sessions_get_distinct_private_scopes(self):
        first = self._record(session_id="td-first", scope_id="auth-first")
        second = self._record(
            session_id="td-second",
            scope_id="auth-second",
            selected_account="alpha",
        )
        first_result = self._rebind(first)
        second_result = self._rebind(second)

        self.assertTrue(first_result.ok, first_result.detail)
        self.assertTrue(second_result.ok, second_result.detail)
        self.assertNotEqual(first_result.new_home, second_result.new_home)
        self.assertEqual(first_result.selected_account_key, "beta")
        self.assertEqual(second_result.selected_account_key, "alpha")
        self.assertEqual(self.store.get(first.id).codex_home, first_result.new_home)
        self.assertEqual(self.store.get(second.id).codex_home, second_result.new_home)

    def test_incomplete_proof_unknown_selection_and_opencode_defer_without_target(self):
        cases = (
            ("proof", self._record(), AuthScopeRebindProof(True, False, True), None),
            (
                "selection",
                self._record(session_id="td-selection", scope_id="auth-selection"),
                None,
                "unknown",
            ),
            (
                "opencode",
                self._record(
                    session_id="td-opencode",
                    scope_id="auth-opencode",
                    command="codex-opencode --yolo",
                    recovery_command=f"codex-opencode --yolo resume {RESUME_ID}",
                ),
                None,
                None,
            ),
        )
        for label, record, proof, selected in cases:
            with self.subTest(label=label):
                old_home = Path(record.codex_home)
                result = self._rebind(record, proof=proof, selected_account=selected)
                self.assertTrue(result.deferred)
                self.assertFalse((default_auth_root(environ=self.env) / record.auth_scope_id).exists())
                self.assertTrue(old_home.is_dir())

    def test_external_session_defers_without_registry_change(self):
        record = self._record(session_id="td-external", external=True)
        old_home = record.codex_home

        result = self._rebind(record)

        self.assertTrue(result.deferred)
        self.assertEqual(self.store.get(record.id).codex_home, old_home)
        self.assertFalse((default_auth_root(environ=self.env) / record.auth_scope_id).exists())

    def test_claim_loss_after_sparse_staging_never_publishes_registry_or_receipt(self):
        record = self._record(session_id="td-claim-loss", scope_id="auth-claim-loss")
        old_home = Path(record.codex_home)
        old_snapshot = _snapshot_tree(old_home)
        claim = self._claim(record)
        original_stage = auth_scope_rebind_module._ensure_new_scope

        def stage_then_drop_claim(*args, **kwargs):
            original_stage(*args, **kwargs)
            self.store.conn.execute(
                "DELETE FROM recovery_claims WHERE session_id = ?",
                (record.id,),
            )
            self.store.conn.commit()

        with mock.patch.object(
            auth_scope_rebind_module,
            "_ensure_new_scope",
            side_effect=stage_then_drop_claim,
        ):
            result = rebind_auth_scope(
                record,
                self.store,
                proof=AuthScopeRebindProof(True, True, True),
                claim=claim,
                canonical_home=self.canonical,
                environ=self.env,
                now=100.0,
            )

        destination = default_auth_root(environ=self.env) / record.auth_scope_id
        self.assertTrue(result.deferred)
        self.assertIn("durable recovery claim", result.detail)
        self.assertEqual(self.store.get(record.id).codex_home, str(old_home))
        self.assertEqual(_snapshot_tree(old_home), old_snapshot)
        self.assertTrue(destination.is_dir())
        self.assertIsNone(result.receipt_path)

    def test_claim_expiry_during_staging_is_rechecked_with_fresh_time(self):
        record = self._record(session_id="td-claim-expiry", scope_id="auth-claim-expiry")
        old_home = Path(record.codex_home)
        claim = self._claim(record)
        self.store.conn.execute(
            "UPDATE recovery_claims SET expires_at = 100.5 WHERE session_id = ?",
            (record.id,),
        )
        self.store.conn.commit()
        current_time = [100.0]
        original_stage = auth_scope_rebind_module._ensure_new_scope

        def stage_past_expiry(*args, **kwargs):
            original_stage(*args, **kwargs)
            current_time[0] = 101.0

        with mock.patch.object(
            auth_scope_rebind_module,
            "_ensure_new_scope",
            side_effect=stage_past_expiry,
        ), mock.patch.object(
            auth_scope_rebind_module.time,
            "time",
            side_effect=lambda: current_time[0],
        ):
            result = rebind_auth_scope(
                record,
                self.store,
                proof=AuthScopeRebindProof(True, True, True),
                claim=claim,
                canonical_home=self.canonical,
                environ=self.env,
            )

        self.assertTrue(result.deferred)
        self.assertIn("expired", result.detail)
        self.assertEqual(self.store.get(record.id).codex_home, str(old_home))
        self.assertIsNone(result.receipt_path)


if __name__ == "__main__":
    unittest.main()
