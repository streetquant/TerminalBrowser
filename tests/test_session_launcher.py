import tempfile
import unittest
from pathlib import Path

from termdeck.command_render import default_shell, directory_tab_name
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
    def test_resolve_defaults_to_directory_name_and_shell(self):
        with tempfile.TemporaryDirectory(prefix="termdeck-client-") as tmp:
            fields = resolve_launch_fields(cwd=tmp)
            self.assertEqual(fields.name, directory_tab_name(tmp))
            self.assertEqual(fields.tag, "terminal")
            self.assertEqual(fields.command, default_shell())
            self.assertEqual(fields.cwd, str(Path(tmp).resolve()))
            self.assertEqual(fields.source, "shell")

    def test_resolve_explicit_command_keeps_directory_name_by_default(self):
        with tempfile.TemporaryDirectory(prefix="termdeck-api-") as tmp:
            fields = resolve_launch_fields(command="python3 -m http.server", cwd=tmp)
            self.assertEqual(fields.name, directory_tab_name(tmp))
            self.assertEqual(fields.tag, "terminal")
            self.assertEqual(fields.command, "python3 -m http.server")
            self.assertEqual(fields.source, "command")

    def test_explicit_name_and_tag_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            fields = resolve_launch_fields(command="python3", name="repl", tag="dev", cwd=tmp)
            self.assertEqual(fields.name, "repl")
            self.assertEqual(fields.tag, "dev")

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
                fields = resolve_launch_fields(cwd=tmp)
                result = create_new_session(store, FakeBackend(), fields, start=False)
                self.assertTrue(result.ok)
                self.assertFalse(result.started)
                self.assertEqual(result.status, "new")
                self.assertIsNone(result.pid)
            finally:
                store.close()

    def test_missing_directory_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "directory does not exist"):
            resolve_launch_fields(cwd="/definitely/not/a/termdeck/directory")


if __name__ == "__main__":
    unittest.main()
