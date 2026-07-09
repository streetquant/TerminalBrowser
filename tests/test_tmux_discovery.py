import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from termdeck.cli import cmd_adopt_tmux
from termdeck.session_store import SessionStore
from termdeck.tmux_discovery import (
    TMUX_PANE_FORMAT,
    adopt_tmux_sessions,
    group_tmux_sessions,
    parse_tmux_panes,
)

SEP = "\x1f"


def row(
    session="legacy",
    session_id="$1",
    attached="1",
    window_index="0",
    window_name="main",
    pane_index="0",
    pane_id="%3",
    pane_pid="1234",
    command="bash",
    cwd="/tmp",
    active="1",
    dead="0",
):
    return SEP.join([
        session,
        session_id,
        attached,
        window_index,
        window_name,
        pane_index,
        pane_id,
        pane_pid,
        command,
        cwd,
        active,
        dead,
    ])


class FakeTmuxBackend:
    available = True

    def __init__(self, text):
        self.text = text
        self.seen_format = None

    def list_panes_text(self, format_string):
        self.seen_format = format_string
        return self.text


class TmuxDiscoveryTests(unittest.TestCase):
    def test_parse_and_group_uses_active_live_pane(self):
        text = "\n".join([
            row(session="work", window_index="0", pane_id="%1", pane_pid="111", command="bash", active="0"),
            row(session="work", window_index="1", window_name="agent", pane_index="0", pane_id="%2", pane_pid="222", command="codex", active="1", cwd="/repo"),
        ])
        panes = parse_tmux_panes(text)
        self.assertEqual(len(panes), 2)
        sessions = group_tmux_sessions(panes)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_name, "work")
        self.assertEqual(sessions[0].command, "codex")
        self.assertEqual(sessions[0].cwd, "/repo")
        self.assertEqual(sessions[0].pane_target, "%2")
        self.assertEqual(sessions[0].pane_count, 2)

    def test_adopt_tmux_sessions_writes_external_record_and_skips_termdeck_owned(self):
        text = "\n".join([
            row(session="legacy", pane_id="%4", pane_pid="444", command="python3", cwd="/tmp/project"),
            row(session="termdeck_ai_codex_td_123", pane_id="%5", pane_pid="555", command="codex", cwd="/tmp"),
        ])
        sessions = group_tmux_sessions(parse_tmux_panes(text))
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                results = adopt_tmux_sessions(store, sessions, tag="legacy", window_id="adopt", window_title="Imported")
                statuses = {result.tmux_name: result.status for result in results}
                self.assertEqual(statuses["legacy"], "adopted")
                self.assertEqual(statuses["termdeck_ai_codex_td_123"], "skipped")
                records = store.list(include_archived=True)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].tmux_name, "legacy")
                self.assertEqual(records[0].tag, "legacy")
                self.assertEqual(records[0].pane_target, "%4")
                self.assertTrue(records[0].external)
                self.assertEqual(records[0].last_pid, 444)
            finally:
                store.close()

    def test_adopt_dry_run_does_not_write_database(self):
        sessions = group_tmux_sessions(parse_tmux_panes(row(session="legacy")))
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                results = adopt_tmux_sessions(store, sessions, dry_run=True)
                self.assertEqual(results[0].status, "planned-adopt")
                self.assertEqual(store.list(include_archived=True), [])
            finally:
                store.close()

    def test_cli_adopt_tmux_uses_hidden_tmux_backend_without_exposing_commands(self):
        text = row(session="legacy", session_id="$42", pane_id="%42", pane_pid="4242", command="bash", cwd="/tmp")

        class FakeBackend(FakeTmuxBackend):
            def __init__(self):
                super().__init__(text)

        with tempfile.TemporaryDirectory() as tmp, patch("termdeck.cli.TmuxBackend", FakeBackend):
            db_path = Path(tmp) / "sessions.sqlite3"
            args = argparse.Namespace(
                db=db_path,
                selectors=[],
                tag="legacy",
                window_id="imported",
                window_title="Imported tmux",
                hidden=False,
                include_termdeck=False,
                dry_run=False,
                list_only=False,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_adopt_tmux(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "adopt-tmux")
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["sessions"][0]["status"], "adopted")

            store = SessionStore(db_path)
            try:
                record = store.list(include_archived=True)[0]
                self.assertEqual(record.tmux_name, "legacy")
                self.assertEqual(record.pane_target, "%42")
                self.assertTrue(record.external)
            finally:
                store.close()

    def test_list_panes_format_has_expected_number_of_fields(self):
        self.assertEqual(len(TMUX_PANE_FORMAT.split(SEP)), 12)


if __name__ == "__main__":
    unittest.main()
