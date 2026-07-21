import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from termdeck.codex_archive import (
    ArchivedCodexSession,
    apply_legacy_recovery,
    plan_legacy_recovery,
    repair_legacy_recovery,
    scan_codex_archive,
)
from termdeck.models import ResourceSnapshot, SessionRecord
from termdeck.session_reconcile import reconcile_store
from termdeck.session_store import SessionStore


SESSION_A = "019f71b3-fa32-7251-bac3-c628f34dd154"
SESSION_B = "019f71b3-b486-7862-a7be-e4e60d20f94c"
SESSION_C = "019f71c2-a4c2-7f42-b148-875dda353e60"


def _record(**changes) -> SessionRecord:
    values = {
        "id": "td-legacy",
        "name": "project",
        "tag": "terminal",
        "command": "/bin/bash",
        "cwd": "/workspace/project",
        "tmux_name": "termdeck_project_td_legacy",
        "created_at": "2026-07-18T02:00:00+00:00",
        "last_opened_at": "2026-07-21T00:00:00+00:00",
        "status": "recovery-pending",
        "visible": True,
        "recovery_command": "codex --yolo",
        "recovery_cwd": "/workspace/project",
    }
    values.update(changes)
    return SessionRecord(**values)


def _archive(
    session_id: str,
    cwd: str,
    *,
    created: str = "2026-07-18T02:00:30Z",
    modified: float = 1_784_340_100.0,
) -> ArchivedCodexSession:
    return ArchivedCodexSession(
        session_id=session_id,
        cwd=cwd,
        created_at=datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp(),
        modified_at=modified,
        path=f"/archive/{session_id}.jsonl",
    )


class CodexArchiveTests(unittest.TestCase):
    def test_scan_reads_only_primary_cli_metadata_and_deduplicates_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary.jsonl"
            duplicate = root / "duplicate.jsonl"
            vscode = root / "vscode.jsonl"
            subagent = root / "subagent.jsonl"
            primary.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": SESSION_A,
                            "source": "cli",
                            "cwd": "/workspace/project",
                            "timestamp": "2026-07-18T02:00:30Z",
                        }
                    }
                )
                + "\nsecret transcript content must not be parsed\n",
                encoding="utf-8",
            )
            duplicate.write_text(primary.read_text(encoding="utf-8"), encoding="utf-8")
            vscode.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": SESSION_B,
                            "source": "vscode",
                            "cwd": "/workspace/project",
                            "timestamp": "2026-07-18T02:00:30Z",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subagent.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": SESSION_C,
                            "source": {"subagent": {}},
                            "cwd": "/workspace/project",
                            "timestamp": "2026-07-18T02:00:30Z",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            duplicate.touch()

            rows = scan_codex_archive(root)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, SESSION_A)
        self.assertEqual(rows[0].cwd, "/workspace/project")

    def test_launcher_only_project_tab_uses_latest_exact_cwd_session(self):
        record = _record()
        older = _archive(SESSION_A, record.cwd, modified=100.0)
        latest = _archive(SESSION_B, record.cwd, modified=200.0)

        decisions = plan_legacy_recovery([record], [older, latest])

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "exact-codex")
        self.assertEqual(decisions[0].session_id, SESSION_B)

    def test_no_launcher_evidence_requires_unique_near_time_match(self):
        record = _record(recovery_command=None, recovery_cwd=None)
        near = _archive(SESSION_A, record.cwd, created="2026-07-18T02:04:00Z")
        far = _archive(
            SESSION_B,
            record.cwd,
            created="2026-07-12T02:00:00Z",
            modified=datetime(2026, 7, 12, 2, 5, tzinfo=timezone.utc).timestamp(),
        )

        decisions = plan_legacy_recovery([record], [far, near])

        self.assertEqual(decisions[0].action, "exact-codex")
        self.assertEqual(decisions[0].session_id, SESSION_A)

    def test_generic_home_ambiguity_falls_back_to_shell(self):
        record = _record(cwd=str(Path.home()), recovery_cwd=str(Path.home()))
        first = _archive(SESSION_A, record.cwd, created="2026-07-12T02:00:00Z")
        second = _archive(SESSION_B, record.cwd, created="2026-07-19T02:00:00Z")

        decisions = plan_legacy_recovery([record], [first, second])

        self.assertEqual(decisions[0].action, "shell-fallback")
        self.assertIsNone(decisions[0].session_id)
        self.assertIn("ambiguous", decisions[0].detail)

    def test_duplicate_uuid_is_resumed_once_and_second_tab_gets_shell(self):
        first = _record(id="td-first", name="project", status="recovery-pending")
        second = _record(id="td-second", name="alias", status="start-error")
        candidate = _archive(SESSION_A, first.cwd)

        decisions = plan_legacy_recovery([second, first], [candidate])
        by_id = {row.record_id: row for row in decisions}

        self.assertEqual(by_id[first.id].action, "exact-codex")
        self.assertEqual(by_id[first.id].session_id, SESSION_A)
        self.assertEqual(by_id[second.id].action, "shell-fallback")

    def test_existing_exact_uuid_is_never_reused_by_legacy_tab(self):
        exact = _record(
            id="td-exact",
            recovery_command=f"codex --yolo resume {SESSION_A}",
        )
        legacy = _record(id="td-legacy-two")
        candidate = _archive(SESSION_A, legacy.cwd)

        decisions = plan_legacy_recovery([exact, legacy], [candidate])

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "shell-fallback")

    def test_apply_persists_exact_uuid_and_makes_shell_startable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                exact = store.create("exact", "terminal", "/bin/bash", "/workspace/exact")
                shell = store.create("shell", "terminal", "/bin/bash", "/workspace/shell")
                store.update(
                    exact.id,
                    status="start-error",
                    recovery_command="codex --yolo",
                    recovery_cwd="/workspace/exact",
                )
                store.update(shell.id, status="recovery-pending", recovery_command="codex --yolo")
                result = apply_legacy_recovery(
                    store,
                    (
                        plan_legacy_recovery(
                            [store.get(exact.id)],  # type: ignore[list-item]
                            [_archive(SESSION_A, "/workspace/exact")],
                        )[0],
                        plan_legacy_recovery(
                            [store.get(shell.id)],  # type: ignore[list-item]
                            [],
                        )[0],
                    ),
                )
                exact_after = store.get(exact.id)
                shell_after = store.get(shell.id)
            finally:
                store.close()

        self.assertEqual(result.exact_count, 1)
        self.assertEqual(result.shell_count, 1)
        self.assertEqual(exact_after.status, "recovery-pending")  # type: ignore[union-attr]
        self.assertEqual(
            exact_after.recovery_command,  # type: ignore[union-attr]
            f"codex --yolo resume {SESSION_A}",
        )
        self.assertEqual(shell_after.status, "new")  # type: ignore[union-attr]
        self.assertEqual(shell_after.recovery_command, "codex --yolo")  # type: ignore[union-attr]

    def test_first_reboot_start_reconciles_running_row_before_archive_repair(self):
        class MissingBackend:
            pane_snapshot_stale = False

            @staticmethod
            def session_exists_many(records):
                return {record.id: False for record in records}

            @staticmethod
            def pane_pids(records):
                return {record.id: None for record in records}

        class MissingSampler:
            @staticmethod
            def snapshots(root_pids, refresh_limit=None):
                return [ResourceSnapshot(pid=None, exists=False) for _pid in root_pids]

            @staticmethod
            def snapshot(_pid):
                return ResourceSnapshot(pid=None, exists=False)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_root = root / "archive"
            archive_root.mkdir()
            (archive_root / "rollout.jsonl").write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": SESSION_A,
                            "source": "cli",
                            "cwd": str(root / "project"),
                            "timestamp": "2026-07-18T02:00:30Z",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create("project", "terminal", "/bin/bash", str(root / "project"))
                store.update(
                    record.id,
                    status="running",
                    recovery_command="codex --yolo",
                    recovery_cwd=str(root / "project"),
                )

                reconcile_store(
                    store,
                    backend=MissingBackend(),
                    sampler=MissingSampler(),
                    hide_missing=True,
                    recover_missing=True,
                )
                pending = store.get(record.id)
                result = repair_legacy_recovery(store, archive_root=archive_root)
                after = store.get(record.id)
            finally:
                store.close()

        self.assertEqual(pending.status, "recovery-pending")  # type: ignore[union-attr]
        self.assertEqual(result.exact_count, 1)
        self.assertEqual(after.recovery_command, f"codex --yolo resume {SESSION_A}")  # type: ignore[union-attr]

    def test_repair_updates_store_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_root = root / "archive"
            archive_root.mkdir()
            metadata = archive_root / "rollout.jsonl"
            metadata.write_text(
                json.dumps(
                    {
                        "payload": {
                            "id": SESSION_A,
                            "source": "cli",
                            "cwd": str(root / "project"),
                            "timestamp": "2026-07-18T02:00:30Z",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create(
                    "project",
                    "terminal",
                    "/bin/bash",
                    str(root / "project"),
                )
                store.update(
                    record.id,
                    status="recovery-pending",
                    recovery_command="codex --yolo",
                    recovery_cwd=str(root / "project"),
                )

                result = repair_legacy_recovery(store, archive_root=archive_root)
                after = store.get(record.id)
            finally:
                store.close()

        self.assertEqual(result.exact_count, 1)
        self.assertEqual(after.recovery_command, f"codex --yolo resume {SESSION_A}")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
