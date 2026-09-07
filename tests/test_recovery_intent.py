from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import termdeck.recovery_intent as recovery_intent_module
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
            source_sidecars_before = {
                path.name for path in source.parent.glob("sessions.sqlite3-*")
            }
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
            self.assertEqual(
                {path.name for path in source.parent.glob("sessions.sqlite3-*")},
                source_sidecars_before,
            )
            self.assertEqual(paths.state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.intent_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            lock_path = paths.lock_dir / f"{session_id}.lock"
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((paths.state_dir / ".recovery-intents.lock").exists())

    def test_pre_migration_legacy_source_must_be_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
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
                    recovery_command="codex --yolo resume active-legacy",
                    recovery_cwd=record.cwd,
                )
                self.assertTrue(Path(f"{source}-wal").exists())
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    return_value=source.parent,
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    with self.assertRaisesRegex(
                        RecoveryIntentError,
                        "active or not durably checkpointed",
                    ):
                        request_recovery_intent(
                            record.id,
                            name=record.name,
                            cwd=record.cwd,
                            environ=self._environment(root),
                        )
                paths = recovery_intent_paths(environ=self._environment(root))
                self.assertEqual(list(paths.intent_dir.iterdir()), [])
            finally:
                store.close()

    def test_managed_only_session_is_authoritative_for_new_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            paths.state_dir.mkdir(mode=0o700)
            store = SessionStore(paths.state_dir / "sessions.sqlite3")
            try:
                record = store.create(
                    "managed-only",
                    "terminal",
                    "/bin/bash",
                    "/8tb/bug-bounty/managed-only",
                )
                store.update(
                    record.id,
                    status="stopped",
                    recovery_command="codex --yolo resume managed-only",
                    recovery_cwd=record.cwd,
                )
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    side_effect=AssertionError("legacy HDD must not be read"),
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    request = request_recovery_intent(
                        record.id,
                        name=record.name,
                        cwd=record.cwd,
                        environ=environment,
                    )
                self.assertFalse(request.idempotent)
                self.assertTrue(
                    (paths.intent_dir / f"{record.id}.json").is_file()
                )
            finally:
                store.close()

    def test_managed_primary_ignores_diverged_or_missing_legacy_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            legacy, session_id = self._source(directory)
            paths = recovery_intent_paths(environ=environment)
            paths.state_dir.mkdir(mode=0o700)
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(legacy, target)
            managed_store = SessionStore(target)
            try:
                managed_store.update(
                    session_id,
                    name="managed-name",
                    tag="managed-tag",
                    cwd="/8tb/bug-bounty/managed-current",
                    recovery_cwd="/8tb/bug-bounty/managed-current",
                    last_opened_at="2026-08-31T12:30:00+00:00",
                )
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    side_effect=AssertionError("legacy HDD must not be read"),
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    request = request_recovery_intent(
                        session_id,
                        name="managed-name",
                        cwd="/8tb/bug-bounty/managed-current",
                        environ=environment,
                    )
                self.assertEqual(request.tag, "managed-tag")
                payload = json.loads(
                    (paths.intent_dir / f"{session_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["name"], "managed-name")
                self.assertEqual(payload["cwd"], "/8tb/bug-bounty/managed-current")
            finally:
                managed_store.close()

            missing_store = SessionStore(target)
            try:
                missing_store.delete(session_id)
            finally:
                missing_store.close()
            (paths.intent_dir / f"{session_id}.json").unlink()
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                side_effect=AssertionError("legacy HDD must not be read"),
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                with self.assertRaisesRegex(
                    RecoveryIntentError,
                    "managed session record was not found",
                ):
                    request_recovery_intent(
                        session_id,
                        name="prompts",
                        cwd="/8tb/bug-bounty/prompts",
                        environ=environment,
                    )

    def test_distinct_session_requests_do_not_block_on_a_legacy_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source = directory / "legacy" / "sessions.sqlite3"
            source.parent.mkdir()
            store = SessionStore(source)
            try:
                first = store.create(
                    "prompts-one",
                    "terminal-one",
                    "/bin/bash",
                    "/8tb/bug-bounty/prompts-one",
                )
                second = store.create(
                    "prompts-two",
                    "terminal-two",
                    "/bin/bash",
                    "/8tb/bug-bounty/prompts-two",
                )
                for record in (first, second):
                    store.update(
                        record.id,
                        status="stopped",
                        recovery_command="codex --yolo resume 01a03ac1-632e-7d60-9265-5e02dbd9b4c0",
                        recovery_cwd=record.cwd,
                    )
                first_id = first.id
                second_id = second.id
            finally:
                store.close()
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            paths.state_dir.mkdir(mode=0o700)
            paths.intent_dir.mkdir(mode=0o700)
            first_read_started = threading.Event()
            release_first_read = threading.Event()
            second_completed = threading.Event()
            results: dict[str, object] = {}
            errors: list[BaseException] = []
            original_read = recovery_intent_module._read_source_row

            def delayed_read(
                session_id: str,
                *,
                expected_name: str,
                expected_cwd: str,
                paths,
            ):
                if session_id == first_id:
                    first_read_started.set()
                    if not release_first_read.wait(2):
                        raise RuntimeError("timed out waiting to release first read")
                return original_read(
                    session_id,
                    expected_name=expected_name,
                    expected_cwd=expected_cwd,
                    paths=paths,
                )

            def run_first() -> None:
                try:
                    results["first"] = request_recovery_intent(
                        first_id,
                        name="prompts-one",
                        cwd="/8tb/bug-bounty/prompts-one",
                        environ=environment,
                    )
                except BaseException as exc:
                    errors.append(exc)

            def run_second() -> None:
                try:
                    results["second"] = request_recovery_intent(
                        second_id,
                        name="prompts-two",
                        cwd="/8tb/bug-bounty/prompts-two",
                        environ=environment,
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    second_completed.set()

            first_thread = threading.Thread(target=run_first)
            second_thread = threading.Thread(target=run_second)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ), mock.patch(
                "termdeck.recovery_intent._read_source_row",
                side_effect=delayed_read,
            ):
                first_thread.start()
                self.assertTrue(first_read_started.wait(2))
                second_thread.start()
                completed_while_first_blocked = second_completed.wait(1)
                release_first_read.set()
                first_thread.join(3)
                second_thread.join(3)
            self.assertTrue(completed_while_first_blocked)
            self.assertFalse(errors)
            self.assertFalse(results["first"].idempotent)
            self.assertFalse(results["second"].idempotent)

    def test_same_session_requests_coalesce_after_concurrent_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source, session_id = self._source(directory)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            paths.state_dir.mkdir(mode=0o700)
            paths.intent_dir.mkdir(mode=0o700)
            source_reads = threading.Barrier(2)
            source_read_state = threading.local()
            results: list[object] = []
            errors: list[BaseException] = []
            original_read = recovery_intent_module._read_source_row

            def concurrent_read(
                read_session_id: str,
                *,
                expected_name: str,
                expected_cwd: str,
                paths,
            ):
                if not getattr(source_read_state, "completed_preflight", False):
                    source_read_state.completed_preflight = True
                    try:
                        source_reads.wait(2)
                    except threading.BrokenBarrierError as exc:
                        raise RuntimeError(
                            "same-session source reads did not overlap"
                        ) from exc
                return original_read(
                    read_session_id,
                    expected_name=expected_name,
                    expected_cwd=expected_cwd,
                    paths=paths,
                )

            def run_request() -> None:
                try:
                    results.append(
                        request_recovery_intent(
                            session_id,
                            name="prompts",
                            cwd="/8tb/bug-bounty/prompts",
                            environ=environment,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            first_thread = threading.Thread(target=run_request)
            second_thread = threading.Thread(target=run_request)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ), mock.patch(
                "termdeck.recovery_intent._read_source_row",
                side_effect=concurrent_read,
            ):
                first_thread.start()
                second_thread.start()
                first_thread.join(3)
                second_thread.join(3)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(len(results), 2)
            self.assertEqual({request.request_id for request in results}, {results[0].request_id})
            self.assertEqual(
                sorted(request.idempotent for request in results),
                [False, True],
            )

    def test_consume_does_not_block_a_distinct_session_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source = directory / "legacy" / "sessions.sqlite3"
            source.parent.mkdir()
            source_store = SessionStore(source)
            try:
                records = [
                    source_store.create(
                        "prompts-one",
                        "terminal-one",
                        "/bin/bash",
                        "/8tb/bug-bounty/prompts-one",
                    ),
                    source_store.create(
                        "prompts-two",
                        "terminal-two",
                        "/bin/bash",
                        "/8tb/bug-bounty/prompts-two",
                    ),
                ]
                for record in records:
                    source_store.update(
                        record.id,
                        status="stopped",
                        recovery_command="codex --yolo resume 01a03ac1-632e-7d60-9265-5e02dbd9b4c0",
                        recovery_cwd=record.cwd,
                    )
                records_by_id = {record.id: record for record in records}
            finally:
                source_store.close()
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            paths.state_dir.mkdir(mode=0o700)
            paths.intent_dir.mkdir(mode=0o700)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                for record in records:
                    request_recovery_intent(
                        record.id,
                        name=record.name,
                        cwd=record.cwd,
                        environ=environment,
                    )
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(source, target)
            managed_store = SessionStore(target)
            try:
                pending = sorted(paths.intent_dir.glob("*.json"), key=lambda path: path.name)
                blocked_path = pending[0]
                other_path = pending[1]
                consume_started = threading.Event()
                release_consume = threading.Event()
                request_completed = threading.Event()
                consume_errors: list[BaseException] = []
                request_results: list[object] = []
                request_errors: list[BaseException] = []

                def delayed_consume(store, path, request, paths):
                    if path == blocked_path:
                        consume_started.set()
                        if not release_consume.wait(2):
                            raise RuntimeError("timed out waiting to release consume")
                    return recovery_intent_module.RecoveryIntentOutcome(
                        request.session_id,
                        "already-active",
                        "test consume",
                    )

                def run_consume() -> None:
                    try:
                        consume_pending_recovery_intents(
                            managed_store,
                            environ=environment,
                        )
                    except BaseException as exc:
                        consume_errors.append(exc)

                def run_request() -> None:
                    try:
                        request_results.append(
                            request_recovery_intent(
                                other_path.stem,
                                name=records_by_id[other_path.stem].name,
                                cwd=records_by_id[other_path.stem].cwd,
                                environ=environment,
                            )
                        )
                    except BaseException as exc:
                        request_errors.append(exc)
                    finally:
                        request_completed.set()

                consume_thread = threading.Thread(target=run_consume)
                request_thread = threading.Thread(target=run_request)
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ), mock.patch(
                    "termdeck.recovery_intent._consume_one",
                    side_effect=delayed_consume,
                ):
                    consume_thread.start()
                    self.assertTrue(consume_started.wait(2))
                    request_thread.start()
                    completed_while_consume_blocked = request_completed.wait(1)
                    release_consume.set()
                    consume_thread.join(3)
                    request_thread.join(3)
                self.assertTrue(completed_while_consume_blocked)
                self.assertFalse(consume_errors)
                self.assertFalse(request_errors)
                self.assertEqual(len(request_results), 1)
                self.assertTrue(request_results[0].idempotent)
            finally:
                managed_store.close()

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
                    self.assertFalse(
                        (paths.lock_dir / f"{session_id}.lock").exists()
                    )
                    self.assertTrue((paths.ack_dir / f"{session_id}.json").exists())
                    self.assertEqual(
                        consume_pending_recovery_intents(
                            store,
                            environ=environment,
                        ),
                        [],
                    )
                finally:
                    store.close()

    def test_consume_quarantines_sidecar_when_fingerprint_changes(self) -> None:
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
                    self.assertFalse(
                        (paths.intent_dir / f"{session_id}.json").exists()
                    )
                    self.assertFalse(
                        (paths.lock_dir / f"{session_id}.lock").exists()
                    )
                    self.assertEqual(len(list(paths.quarantine_dir.iterdir())), 1)
                    self.assertNotEqual(store.get(session_id).recovery_intent, "active")
                finally:
                    store.close()

    def test_partial_retirement_marker_is_repaired_per_session(self) -> None:
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
                original = request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
                paths = recovery_intent_paths(environ=environment)
                lock = paths.lock_dir / f"{session_id}.lock"
                lock.write_bytes(recovery_intent_module._RETIRED_LOCK_MARKER[:11])
                repeated = request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
            self.assertEqual(repeated.request_id, original.request_id)
            self.assertTrue(repeated.idempotent)
            self.assertEqual(lock.read_bytes(), b"")

    def test_consumer_busy_session_does_not_block_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source = directory / "legacy" / "sessions.sqlite3"
            source.parent.mkdir()
            source_store = SessionStore(source)
            try:
                records = [
                    source_store.create(
                        f"prompts-{index}",
                        f"terminal-{index}",
                        "/bin/bash",
                        f"/8tb/bug-bounty/prompts-{index}",
                    )
                    for index in range(2)
                ]
                for record in records:
                    source_store.update(
                        record.id,
                        status="stopped",
                        recovery_command="codex --yolo resume 01a03ac1-632e-7d60-9265-5e02dbd9b4c0",
                        recovery_cwd=record.cwd,
                    )
            finally:
                source_store.close()
            environment = self._environment(root)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                for record in records:
                    request_recovery_intent(
                        record.id,
                        name=record.name,
                        cwd=record.cwd,
                        environ=environment,
                    )
            paths = recovery_intent_paths(environ=environment)
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(source, target)
            store = SessionStore(target)
            locked_id = sorted(record.id for record in records)[0]
            available_id = sorted(record.id for record in records)[1]
            lock = paths.lock_dir / f"{locked_id}.lock"
            descriptor = os.open(lock, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                started = time.monotonic()
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                elapsed = time.monotonic() - started
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            try:
                states = {outcome.session_id: outcome.state for outcome in outcomes}
                self.assertEqual(states[locked_id], "pending")
                self.assertEqual(states[available_id], "applied")
                self.assertLess(elapsed, 0.5)
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    retried = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                self.assertEqual(
                    [(outcome.session_id, outcome.state) for outcome in retried],
                    [(locked_id, "applied")],
                )
            finally:
                store.close()

    def test_request_overlapping_consume_returns_terminal_acknowledgement(self) -> None:
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
                original_request = request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
            paths = recovery_intent_paths(environ=environment)
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(source, target)
            commit_reached = threading.Event()
            release_consume = threading.Event()
            request_started = threading.Event()
            consume_results: list[object] = []
            request_results: list[object] = []
            errors: list[BaseException] = []
            original_write_ack = recovery_intent_module._write_ack

            def delayed_write_ack(*args, **kwargs):
                commit_reached.set()
                if not release_consume.wait(2):
                    raise RuntimeError("timed out waiting to publish acknowledgement")
                return original_write_ack(*args, **kwargs)

            def run_consume() -> None:
                thread_store = SessionStore(target)
                try:
                    consume_results.extend(
                        consume_pending_recovery_intents(
                            thread_store,
                            environ=environment,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    thread_store.close()

            def run_request() -> None:
                request_started.set()
                try:
                    request_results.append(
                        request_recovery_intent(
                            session_id,
                            name="prompts",
                            cwd="/8tb/bug-bounty/prompts",
                            environ=environment,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            consume_thread = threading.Thread(target=run_consume)
            request_thread = threading.Thread(target=run_request)
            try:
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    return_value=source.parent,
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ), mock.patch(
                    "termdeck.recovery_intent._write_ack",
                    side_effect=delayed_write_ack,
                ):
                    consume_thread.start()
                    self.assertTrue(commit_reached.wait(2))
                    request_thread.start()
                    self.assertTrue(request_started.wait(1))
                    time.sleep(0.05)
                    self.assertTrue(request_thread.is_alive())
                    release_consume.set()
                    consume_thread.join(3)
                    request_thread.join(3)
                self.assertFalse(consume_thread.is_alive())
                self.assertFalse(request_thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual([result.state for result in consume_results], ["applied"])
                self.assertEqual(len(request_results), 1)
                acknowledgement = request_results[0]
                self.assertEqual(acknowledgement.request_id, original_request.request_id)
                self.assertTrue(acknowledgement.idempotent)
                self.assertFalse(acknowledgement.queued)
                self.assertFalse((paths.intent_dir / f"{session_id}.json").exists())
                self.assertTrue((paths.ack_dir / f"{session_id}.json").exists())
                verification_store = SessionStore(target)
                try:
                    self.assertEqual(
                        verification_store.get(session_id).recovery_intent,
                        "active",
                    )
                finally:
                    verification_store.close()
            finally:
                release_consume.set()
                if consume_thread.is_alive():
                    consume_thread.join(3)
                if request_thread.is_alive():
                    request_thread.join(3)

    def test_terminal_acknowledgement_outranks_retained_pending_sidecar(self) -> None:
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
                original = request_recovery_intent(
                    session_id,
                    name="prompts",
                    cwd="/8tb/bug-bounty/prompts",
                    environ=environment,
                )
            paths = recovery_intent_paths(environ=environment)
            sidecar = paths.intent_dir / f"{session_id}.json"
            retained_payload = sidecar.read_bytes()
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(source, target)
            store = SessionStore(target)
            try:
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                self.assertEqual([outcome.state for outcome in outcomes], ["applied"])
                sidecar.write_bytes(retained_payload)
                sidecar.chmod(0o600)
                with mock.patch(
                    "termdeck.recovery_intent.legacy_state_dir",
                    side_effect=AssertionError("terminal ack must avoid source reread"),
                ), mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    repeated = request_recovery_intent(
                        session_id,
                        name="prompts",
                        cwd="/8tb/bug-bounty/prompts",
                        environ=environment,
                    )
                self.assertEqual(repeated.request_id, original.request_id)
                self.assertTrue(repeated.idempotent)
                self.assertFalse(repeated.queued)
            finally:
                store.close()

    def test_stale_acknowledgement_never_masks_changed_managed_identity(self) -> None:
        cases = (
            {"tag": "changed-tag"},
            {"name": "renamed", "cwd": "/8tb/bug-bounty/renamed"},
        )
        for changed_fields in cases:
            with self.subTest(changed_fields=changed_fields), tempfile.TemporaryDirectory() as temporary:
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
                    original_request = request_recovery_intent(
                        session_id,
                        name="prompts",
                        cwd="/8tb/bug-bounty/prompts",
                        environ=environment,
                    )
                paths = recovery_intent_paths(environ=environment)
                target = paths.state_dir / "sessions.sqlite3"
                shutil.copy2(source, target)
                managed_store = SessionStore(target)
                try:
                    with mock.patch(
                        "termdeck.recovery_intent.managed_autostart_ready",
                        return_value=(True, "verified"),
                    ):
                        consumed = consume_pending_recovery_intents(
                            managed_store,
                            environ=environment,
                        )
                    self.assertEqual([outcome.state for outcome in consumed], ["applied"])
                    changes = {
                        **changed_fields,
                        "last_opened_at": "2026-08-31T12:00:00+00:00",
                    }
                    source_store = SessionStore(source)
                    try:
                        source_store.update(session_id, **changes)
                    finally:
                        source_store.close()
                    managed_store.update(session_id, **changes)
                    expected_name = str(changed_fields.get("name", "prompts"))
                    expected_cwd = str(
                        changed_fields.get("cwd", "/8tb/bug-bounty/prompts")
                    )
                    with mock.patch(
                        "termdeck.recovery_intent.legacy_state_dir",
                        return_value=source.parent,
                    ), mock.patch(
                        "termdeck.recovery_intent.managed_autostart_ready",
                        return_value=(True, "verified"),
                    ):
                        replacement = request_recovery_intent(
                            session_id,
                            name=expected_name,
                            cwd=expected_cwd,
                            environ=environment,
                        )
                    self.assertNotEqual(replacement.request_id, original_request.request_id)
                    self.assertFalse(replacement.idempotent)
                    self.assertTrue(replacement.queued)
                    self.assertFalse((paths.ack_dir / f"{session_id}.json").exists())
                    self.assertTrue((paths.intent_dir / f"{session_id}.json").exists())
                finally:
                    managed_store.close()

    def test_more_than_256_pending_intents_drain_without_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            store = SessionStore(paths.state_dir / "sessions.sqlite3")
            total = recovery_intent_module._MAX_PENDING_FILES + 44
            try:
                for index in range(total):
                    session_id = f"session-{index:04d}"
                    payload = {
                        "schema": recovery_intent_module.RECOVERY_INTENT_SCHEMA,
                        "intent": "active",
                        "request_id": str(recovery_intent_module.uuid.uuid4()),
                        "created_at": "2026-08-31T00:00:00+00:00",
                        "session_id": session_id,
                        "name": f"session {index}",
                        "tag": "terminal",
                        "cwd": f"/8tb/workspace-{index}",
                        "tmux_name": f"session-{index}",
                        "external": 0,
                        "fingerprint": "0" * 64,
                    }
                    sidecar = paths.intent_dir / f"{session_id}.json"
                    sidecar.write_text(
                        recovery_intent_module._canonical_json(payload),
                        encoding="utf-8",
                    )
                    sidecar.chmod(0o600)

                def consume_stub(store, path, request, paths):
                    time.sleep(0.002)
                    path.unlink()
                    return recovery_intent_module.RecoveryIntentOutcome(
                        request.session_id,
                        "applied",
                        "test consume",
                    )

                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ), mock.patch(
                    "termdeck.recovery_intent._consume_one",
                    side_effect=consume_stub,
                ), mock.patch(
                    "termdeck.recovery_intent._fsync_directory",
                ):
                    outcomes = []
                    call_elapsed: list[float] = []
                    while recovery_intent_module.recovery_intent_backlog_pending(
                        environ=environment
                    ):
                        started = time.monotonic()
                        batch = consume_pending_recovery_intents(
                            store,
                            environ=environment,
                            time_budget_seconds=0.02,
                        )
                        call_elapsed.append(time.monotonic() - started)
                        outcomes.extend(batch)
                        self.assertTrue(batch)
                self.assertEqual(len(outcomes), total)
                self.assertEqual({outcome.state for outcome in outcomes}, {"applied"})
                self.assertEqual(list(paths.intent_dir.glob("*.json")), [])
                self.assertGreater(len(call_elapsed), 1)
                self.assertLess(max(call_elapsed), 0.08)
            finally:
                store.close()

    def test_bounded_directory_cursor_reaches_every_entry_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            total = recovery_intent_module._MAX_PENDING_FILES + 44
            expected = {f"session-{index:04d}.json" for index in range(total)}
            for name in expected:
                artifact = paths.intent_dir / name
                artifact.write_text("pending", encoding="utf-8")
                artifact.chmod(0o600)

            first, first_exhausted = (
                recovery_intent_module._bounded_directory_entries(
                    paths.intent_dir,
                    limit=recovery_intent_module._MAX_PENDING_FILES,
                )
            )
            second, second_exhausted = (
                recovery_intent_module._bounded_directory_entries(
                    paths.intent_dir,
                    limit=recovery_intent_module._MAX_PENDING_FILES,
                )
            )

            self.assertFalse(first_exhausted)
            self.assertTrue(second_exhausted)
            self.assertEqual(len(first), recovery_intent_module._MAX_PENDING_FILES)
            self.assertEqual(len(second), total - len(first))
            self.assertEqual({path.name for path in first + second}, expected)

    def test_malformed_sidecar_is_quarantined_without_blocking_valid_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source = directory / "legacy" / "sessions.sqlite3"
            source.parent.mkdir()
            source_store = SessionStore(source)
            try:
                records = [
                    source_store.create(
                        f"prompts-{index}",
                        f"terminal-{index}",
                        "/bin/bash",
                        f"/8tb/bug-bounty/prompts-{index}",
                    )
                    for index in range(2)
                ]
                for record in records:
                    source_store.update(
                        record.id,
                        status="stopped",
                        recovery_command="codex --yolo resume 01a03ac1-632e-7d60-9265-5e02dbd9b4c0",
                        recovery_cwd=record.cwd,
                    )
            finally:
                source_store.close()
            environment = self._environment(root)
            with mock.patch(
                "termdeck.recovery_intent.legacy_state_dir",
                return_value=source.parent,
            ), mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                for record in records:
                    request_recovery_intent(
                        record.id,
                        name=record.name,
                        cwd=record.cwd,
                        environ=environment,
                    )
            paths = recovery_intent_paths(environ=environment)
            malformed_id = sorted(record.id for record in records)[0]
            valid_id = sorted(record.id for record in records)[1]
            malformed = paths.intent_dir / f"{malformed_id}.json"
            malformed.write_bytes(b"{")
            target = paths.state_dir / "sessions.sqlite3"
            shutil.copy2(source, target)
            store = SessionStore(target)
            try:
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                states = {outcome.session_id: outcome.state for outcome in outcomes}
                self.assertEqual(states[malformed_id], "rejected")
                self.assertEqual(states[valid_id], "applied")
                self.assertFalse(malformed.exists())
                self.assertEqual(len(list(paths.quarantine_dir.iterdir())), 1)
            finally:
                store.close()

    def test_residue_quarantine_is_time_bounded_and_completes_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            store = SessionStore(paths.state_dir / "sessions.sqlite3")
            for index in range(80):
                residue = paths.intent_dir / f".orphan-{index:03d}.tmp"
                residue.write_text("partial", encoding="utf-8")
                residue.chmod(0o600)
            original_quarantine = recovery_intent_module._quarantine_artifact

            def slow_quarantine(path, private_paths):
                time.sleep(0.002)
                return original_quarantine(path, private_paths)

            try:
                elapsed: list[float] = []
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ), mock.patch(
                    "termdeck.recovery_intent._quarantine_artifact",
                    side_effect=slow_quarantine,
                ), mock.patch(
                    "termdeck.recovery_intent._fsync_directory",
                ):
                    while recovery_intent_module.recovery_intent_backlog_pending(
                        environ=environment
                    ):
                        started = time.monotonic()
                        consume_pending_recovery_intents(
                            store,
                            environ=environment,
                            time_budget_seconds=0.02,
                        )
                        elapsed.append(time.monotonic() - started)
                self.assertGreater(len(elapsed), 1)
                self.assertLess(max(elapsed), 0.08)
                self.assertEqual(list(paths.intent_dir.iterdir()), [])
                self.assertEqual(len(list(paths.quarantine_dir.iterdir())), 80)
            finally:
                store.close()

    def test_orphan_lock_and_temporary_cleanup_is_per_session_and_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            store = SessionStore(paths.state_dir / "sessions.sqlite3")
            free_session = "td-orphan-free"
            busy_session = "td-orphan-busy"
            free_lock = paths.lock_dir / f"{free_session}.lock"
            busy_lock = paths.lock_dir / f"{busy_session}.lock"
            for lock_path in (free_lock, busy_lock):
                lock_path.write_bytes(
                    recovery_intent_module._RETIRED_LOCK_MARKER[:9]
                )
                lock_path.chmod(0o600)
            free_temporary = recovery_intent_module._temporary_path(
                paths,
                free_session,
                kind="intent",
            )
            busy_temporary = recovery_intent_module._temporary_path(
                paths,
                busy_session,
                kind="ack",
            )
            for temporary_path in (free_temporary, busy_temporary):
                temporary_path.write_text("partial", encoding="utf-8")
                temporary_path.chmod(0o600)
            busy_descriptor = os.open(busy_lock, os.O_RDWR | os.O_CLOEXEC)
            fcntl.flock(busy_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                started = time.monotonic()
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    self.assertEqual(
                        consume_pending_recovery_intents(
                            store,
                            environ=environment,
                            time_budget_seconds=0.05,
                        ),
                        [],
                    )
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertFalse(free_lock.exists())
                self.assertFalse(free_temporary.exists())
                self.assertTrue(busy_lock.exists())
                self.assertTrue(busy_temporary.exists())
            finally:
                fcntl.flock(busy_descriptor, fcntl.LOCK_UN)
                os.close(busy_descriptor)
            try:
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ):
                    consume_pending_recovery_intents(
                        store,
                        environ=environment,
                        time_budget_seconds=0.05,
                    )
                self.assertFalse(busy_lock.exists())
                self.assertFalse(busy_temporary.exists())
                self.assertFalse(
                    recovery_intent_module.recovery_intent_backlog_pending(
                        environ=environment
                    )
                )
            finally:
                store.close()

    def test_failed_rejection_quarantine_is_retryable_not_false_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            store = SessionStore(paths.state_dir / "sessions.sqlite3")
            session_id = "td-missing"
            sidecar = paths.intent_dir / f"{session_id}.json"
            payload = {
                "schema": recovery_intent_module.RECOVERY_INTENT_SCHEMA,
                "intent": "active",
                "request_id": "00000000-0000-4000-8000-000000000001",
                "created_at": "2026-08-31T00:00:00+00:00",
                "session_id": session_id,
                "name": "missing",
                "tag": "terminal",
                "cwd": "/tmp/missing",
                "tmux_name": "termdeck_terminal_missing",
                "external": 0,
                "fingerprint": "0" * 64,
            }
            sidecar.write_text(
                recovery_intent_module._canonical_json(payload),
                encoding="utf-8",
            )
            sidecar.chmod(0o600)
            try:
                with mock.patch(
                    "termdeck.recovery_intent.managed_autostart_ready",
                    return_value=(True, "verified"),
                ), mock.patch(
                    "termdeck.recovery_intent._quarantine_artifact",
                    side_effect=RecoveryIntentError("injected quarantine failure"),
                ):
                    outcomes = consume_pending_recovery_intents(
                        store,
                        environ=environment,
                    )
                self.assertEqual([outcome.state for outcome in outcomes], ["pending"])
                self.assertIn("quarantine failed", outcomes[0].detail)
                self.assertTrue(sidecar.exists())
            finally:
                store.close()

    def test_stale_temporary_symlink_is_quarantined_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "runtime" / "t1000-managed"
            root.mkdir(parents=True)
            source, session_id = self._source(directory)
            environment = self._environment(root)
            paths = recovery_intent_paths(environ=environment)
            with mock.patch(
                "termdeck.recovery_intent.managed_autostart_ready",
                return_value=(True, "verified"),
            ):
                recovery_intent_module._prepare_private_paths(paths)
            victim = directory / "victim"
            victim.write_text("unchanged", encoding="utf-8")
            temporary_path = recovery_intent_module._temporary_path(
                paths,
                session_id,
                kind="intent",
            )
            temporary_path.symlink_to(victim)
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
            self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse(temporary_path.exists())
            self.assertEqual(list(paths.temporary_dir.iterdir()), [])
            self.assertEqual(len(list(paths.quarantine_dir.iterdir())), 1)
            for _ in range(3):
                temporary_path.symlink_to(victim)
                recovery_intent_module._quarantine_artifact(temporary_path, paths)
            self.assertEqual(len(list(paths.quarantine_dir.iterdir())), 1)

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
                "termdeck.cli.require_hot_storage"
            ):
                _prepare_managed_session_database(args)
            self.assertIsNone(args.db)


if __name__ == "__main__":
    unittest.main()
