from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import MasterWindow
from termdeck.models import SessionRecord


class CountingBackend:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self, record: SessionRecord, lines: int = 80) -> str:
        self.calls += 1
        return f"capture {self.calls} for {record.id} lines={lines}"


def make_record(*, status: str = "running", last_pid: int | None = 1234) -> SessionRecord:
    return SessionRecord(
        id="session-1",
        name="dev",
        tag="work",
        command="bash",
        cwd="/tmp",
        tmux_name="termdeck-session-1",
        created_at="2026-07-09T00:00:00Z",
        last_opened_at="2026-07-09T00:00:00Z",
        status=status,
        last_pid=last_pid,
    )


def make_master(backend: CountingBackend) -> MasterWindow:
    master = MasterWindow.__new__(MasterWindow)
    master.app_context = SimpleNamespace(backend=backend)
    master._capture_preview_cache = {}
    master._capture_preview_bucket_seconds = 3.0
    return master


class MasterPreviewCacheTest(TestCase):
    def test_reuses_cached_capture_inside_same_preview_bucket(self) -> None:
        backend = CountingBackend()
        master = make_master(backend)
        record = make_record()

        with patch("termdeck.gtk_app.time.monotonic", return_value=9.2):
            first = master.capture_preview_text(record)
            second = master.capture_preview_text(record)

        self.assertEqual(first, second)
        self.assertEqual(backend.calls, 1)

    def test_recaptures_when_preview_bucket_or_session_state_changes(self) -> None:
        backend = CountingBackend()
        master = make_master(backend)
        running = make_record(status="running", last_pid=1234)
        paused = make_record(status="paused", last_pid=1234)

        with patch("termdeck.gtk_app.time.monotonic", return_value=9.2):
            first = master.capture_preview_text(running)
            same_bucket = master.capture_preview_text(running)
            state_changed = master.capture_preview_text(paused)
        with patch("termdeck.gtk_app.time.monotonic", return_value=12.2):
            next_bucket = master.capture_preview_text(paused)

        self.assertEqual(first, same_bucket)
        self.assertNotEqual(first, state_changed)
        self.assertNotEqual(state_changed, next_bucket)
        self.assertEqual(backend.calls, 3)


if __name__ == "__main__":
    import unittest

    unittest.main()
