import os
import tempfile
import unittest
from pathlib import Path

from termdeck.session_launcher import create_new_session, resolve_launch_fields
from termdeck.session_store import SessionStore


class FakeBackend:
    available = True

    def __init__(self):
        self.started = []

    def create_or_resume(self, record):
        self.started.append(record.tmux_name)

    def pane_pid(self, record):
        return 4242


class SessionLauncherTests(unittest.TestCase):
    def test_resolve_uses_template_when_target_matches_template_id(self):
        fields = resolve_launch_fields(target="codex", cwd="/tmp")
        self.assertEqual(fields.name, "codex")
        self.assertEqual(fields.tag, "ai")
        self.assertEqual(fields.command, "codex")
        self.assertEqual(fields.source, "template:codex")

    def test_resolve_custom_command_overrides_template(self):
        fields = resolve_launch_fields(template_id="shell", command="python3 -m http.server", name="web", tag="dev")
        self.assertEqual(fields.name, "web")
        self.assertEqual(fields.tag, "dev")
        self.assertEqual(fields.command, "python3 -m http.server")
        self.assertEqual(fields.source, "command")

    def test_create_new_session_assigns_next_tab_index_and_can_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                store.create("first", "dev", "bash", tmp, window_id="win", tab_index=3)
                fields = resolve_launch_fields(command="python3", name="py", tag="repl", cwd=tmp)
                backend = FakeBackend()
                result = create_new_session(store, backend, fields, window_id="win", window_title="Dev", start=True)
                self.assertTrue(result.ok)
                self.assertTrue(result.started)
                self.assertEqual(result.pid, 4242)
                self.assertEqual(result.tab_index, 4)
                self.assertEqual(result.status, "running")
                self.assertEqual(len(backend.started), 1)
            finally:
                store.close()

    def test_create_new_session_no_start_keeps_new_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                fields = resolve_launch_fields(target="shell", cwd=tmp)
                result = create_new_session(store, FakeBackend(), fields, start=False)
                self.assertTrue(result.ok)
                self.assertFalse(result.started)
                self.assertEqual(result.status, "new")
                self.assertEqual(result.pid, None)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
