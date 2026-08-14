from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from termdeck.freeze_diagnostics import (
    PressureSample,
    auth_scope_footprints,
    blocked_processes,
    build_freeze_diagnostic_payload,
    read_io_pressure,
)


class FreezeDiagnosticsTests(TestCase):
    def test_io_pressure_parser_keeps_some_and_full_windows_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "io.pressure"
            path.write_text(
                "some avg10=17.84 avg60=16.38 avg300=18.43 total=100\n"
                "full avg10=17.03 avg60=15.41 avg300=17.18 total=90\n",
                encoding="utf-8",
            )

            sample = read_io_pressure(path)

        self.assertEqual(sample.some_avg10, 17.84)
        self.assertEqual(sample.some_avg60, 16.38)
        self.assertEqual(sample.full_avg10, 17.03)
        self.assertEqual(sample.full_avg300, 17.18)

    def test_blocked_process_scan_is_bounded_and_reports_only_d_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dstate = root / "100"
            running = root / "101"
            dstate.mkdir()
            running.mkdir()
            dstate.joinpath("stat").write_text(
                "100 (jbd2/sdb1-8) D 1 2 3 4 5\n", encoding="utf-8"
            )
            dstate.joinpath("wchan").write_text("io_schedule\n", encoding="utf-8")
            running.joinpath("stat").write_text(
                "101 (python) R 1 2 3 4 5\n", encoding="utf-8"
            )
            running.joinpath("wchan").write_text("0\n", encoding="utf-8")

            rows = blocked_processes(proc_root=root, max_processes=2, max_rows=2)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pid, 100)
        self.assertEqual(rows[0].comm, "jbd2/sdb1-8")
        self.assertEqual(rows[0].wait_channel, "io_schedule")

    def test_auth_scope_footprint_reads_only_direct_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = root / "terminal-auth"
            auth_root.mkdir(mode=0o700)
            referenced = auth_root / ("auth-" + "1" * 32)
            orphan = auth_root / ("auth-" + "2" * 32)
            for home in (referenced, orphan):
                home.mkdir(mode=0o700)
            referenced.joinpath("state_5.sqlite").write_bytes(b"a" * 11)
            orphan.joinpath("state_5.sqlite").write_bytes(b"b" * 7)
            orphan.joinpath("state_5.sqlite-wal").write_bytes(b"c" * 5)
            nested = orphan / "sessions" / "large"
            nested.mkdir(parents=True)
            nested.joinpath("ignored.bin").write_bytes(b"x" * 1000)
            db = root / "sessions.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.execute("CREATE TABLE sessions(id TEXT, codex_home TEXT)")
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    ("one", str(referenced.resolve())),
                )
                connection.commit()
            finally:
                connection.close()

            rows = auth_scope_footprints(db_path=db, auth_root=auth_root)

        by_id = {row.scope_id: row for row in rows}
        self.assertTrue(by_id[referenced.name].referenced)
        self.assertEqual(by_id[referenced.name].state_bytes, 11)
        self.assertFalse(by_id[orphan.name].referenced)
        self.assertEqual(by_id[orphan.name].state_bytes, 7)
        self.assertEqual(by_id[orphan.name].wal_bytes, 5)
        self.assertEqual(by_id[orphan.name].shm_bytes, 0)

    def test_freeze_payload_excludes_environment_arguments_and_terminal_captures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_root = root / "terminal-auth"
            auth_root.mkdir(mode=0o700)
            db = root / "sessions.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.execute("CREATE TABLE sessions(id TEXT, codex_home TEXT)")
                connection.commit()
            finally:
                connection.close()

            with mock.patch(
                "termdeck.freeze_diagnostics.read_io_pressure",
                return_value=PressureSample(full_avg10=42.5),
            ), mock.patch(
                "termdeck.freeze_diagnostics.blocked_processes", return_value=()
            ), mock.patch(
                "termdeck.freeze_diagnostics._tmux_latency_ms", return_value=(8.0, "")
            ):
                payload = build_freeze_diagnostic_payload(
                    db_path=db,
                    auth_root=auth_root,
                )

        self.assertEqual(payload["schema"], "termdeck.freeze-diagnostics.v1")
        self.assertEqual(payload["io_pressure"]["full_avg10"], 42.5)
        self.assertEqual(payload["tmux_control_latency_ms"], 8.0)
        self.assertFalse(payload["privacy"]["environment_included"])
        self.assertFalse(payload["privacy"]["terminal_captures_included"])
        self.assertFalse(payload["privacy"]["process_arguments_included"])
