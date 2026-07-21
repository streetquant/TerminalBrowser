import tempfile
import unittest
from pathlib import Path

from termdeck.checkpointing import CheckpointResult
from termdeck.persistence import persist_live_sessions, persistence_candidates
from termdeck.session_store import SessionStore


class RecordingBackend:
    def __init__(self, recovery):
        self.recovery = recovery
        self.bounded_values = []
        self.checkpoint_records_seen = []

    def recovery_metadata(self, records, *, bounded=True):
        self.bounded_values.append(bounded)
        return self.recovery

    def checkpoint_records(self, records):
        self.checkpoint_records_seen = list(records)
        return [
            CheckpointResult(
                record.id,
                "codex-resume" if " resume " in (record.recovery_command or "") else "checkpoint",
                True,
                True,
                "saved",
                0 if " resume " in (record.recovery_command or "") else 1,
            )
            for record in records
        ]


class PersistenceTests(unittest.TestCase):
    def test_final_unbounded_codex_observation_precedes_checkpoint_selection(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("agent", "ai", "/bin/bash", tmp)
                store.update(record.id, status="running", last_pid=101)
                backend = RecordingBackend(
                    {
                        record.id: (
                            f"codex --yolo resume {session_id}",
                            tmp,
                        )
                    }
                )

                result = persist_live_sessions(store, backend)
                refreshed = store.get(record.id)

                self.assertEqual(backend.bounded_values, [False])
                self.assertEqual(result.metadata_updates, 1)
                self.assertEqual(result.exact_count, 1)
                self.assertEqual(result.failed_count, 0)
                self.assertEqual(
                    refreshed.recovery_command,  # type: ignore[union-attr]
                    f"codex --yolo resume {session_id}",
                )
                self.assertEqual(
                    backend.checkpoint_records_seen[0].recovery_command,
                    f"codex --yolo resume {session_id}",
                )
            finally:
                store.close()

    def test_shutdown_observation_never_erases_persisted_exact_codex_uuid(self):
        session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("agent", "ai", "/bin/bash", tmp)
                store.update(record.id, status="running", last_pid=101)
                store.update_recovery_metadata(
                    [(record.id, f"codex --yolo resume {session_id}", tmp)]
                )
                backend = RecordingBackend({record.id: (None, None)})

                result = persist_live_sessions(store, backend)
                refreshed = store.get(record.id)

                self.assertEqual(result.metadata_updates, 0)
                self.assertEqual(result.exact_count, 1)
                self.assertEqual(
                    refreshed.recovery_command,  # type: ignore[union-attr]
                    f"codex --yolo resume {session_id}",
                )
                self.assertEqual(
                    backend.checkpoint_records_seen[0].recovery_command,
                    f"codex --yolo resume {session_id}",
                )
            finally:
                store.close()

    def test_candidates_exclude_stopped_archived_and_external_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                running = store.create("running", "dev", "python worker.py", tmp)
                stopped = store.create("stopped", "dev", "python stopped.py", tmp)
                archived = store.create("archived", "dev", "python archived.py", tmp)
                external = store.create("external", "dev", "python external.py", tmp)
                store.update(running.id, status="running")
                store.update(stopped.id, status="stopped")
                store.update(archived.id, status="archived")
                store.update(external.id, status="running", external=True)

                selected = persistence_candidates(store)

                self.assertEqual([record.id for record in selected], [running.id])
            finally:
                store.close()

    def test_checkpoint_exception_is_reported_for_every_selected_record(self):
        class FailingBackend:
            def recovery_metadata(self, records, *, bounded=True):
                return {}

            def checkpoint_records(self, records):
                raise RuntimeError("storage unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "dev", "python first.py", tmp)
                second = store.create("second", "dev", "python second.py", tmp)
                store.update(first.id, status="running")
                store.update(second.id, status="running")

                result = persist_live_sessions(store, FailingBackend())

                self.assertEqual(result.failed_count, 2)
                self.assertTrue(
                    all("storage unavailable" in row.detail for row in result.checkpoints)
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
