"""Offline tests for :mod:`termdeck.pane_stream` with an injected fake control client.

The fake client answers commands synchronously from a one-shot prefix script,
so these tests exercise the controller's real worker thread, ready gate,
replay assembly, and setup retry logic without any tmux server.
"""

from __future__ import annotations

import threading
import time
import unittest

from termdeck.pane_stream import PaneStreamController, _STATE_FIELDS
from termdeck.tmux_control import ClientState, CommandResult, ControlEvent


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def state_line(history_size: int = 0, **overrides: str) -> str:
    values = {
        "pane_id": "%1",
        "alternate_on": "0",
        "pane_dead": "0",
        "cursor_x": "0",
        "cursor_y": "0",
        "cursor_flag": "1",
        "keypad_cursor_flag": "0",
        "wrap_flag": "1",
        "insert_flag": "0",
        "origin_flag": "0",
        "mouse_standard_flag": "0",
        "mouse_button_flag": "0",
        "mouse_all_flag": "0",
        "mouse_sgr_flag": "0",
        "history_size": str(history_size),
    }
    values.update(overrides)
    return " ".join(values[name] for name in _STATE_FIELDS)


class FakeControlClient:
    """Synchronous scripted stand-in for ControlModeClient."""

    def __init__(self, **kwargs) -> None:
        self._on_state_change = kwargs["on_state_change"]
        self.on_event = kwargs["on_event"]
        self._lock = threading.Lock()
        self.commands: list[str] = []
        self._script: list[tuple[str, CommandResult]] = []
        self.state = ClientState.CONNECTING
        self.started = False
        self.stopped = False

    def script(self, prefix: str, result: CommandResult) -> None:
        with self._lock:
            self._script.append((prefix, result))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def connect(self) -> None:
        self.state = ClientState.CONNECTED
        self._on_state_change(ClientState.CONNECTED)

    def send_command(self, command: str, *, timeout: float | None = None) -> CommandResult:
        with self._lock:
            self.commands.append(command)
            for index, (prefix, result) in enumerate(self._script):
                if command.startswith(prefix):
                    del self._script[index]
                    return result
        return CommandResult(True, (), None)

    def count(self, fragment: str) -> int:
        with self._lock:
            return sum(1 for command in self.commands if fragment in command)


class PaneStreamControllerTests(unittest.TestCase):
    def make_controller(self, **kwargs):
        outputs: list[bytes] = []
        created: dict[str, FakeControlClient] = {}

        def factory(**factory_kwargs) -> FakeControlClient:
            client = FakeControlClient(**factory_kwargs)
            created["client"] = client
            return client

        controller = PaneStreamController(
            attach_argv=["tmux", "attach-session", "-t", "demo"],
            on_output=outputs.append,
            client_factory=factory,
            **kwargs,
        )
        self.addCleanup(controller.stop)
        return controller, created["client"], outputs

    def test_zero_history_size_skips_the_history_capture(self) -> None:
        controller, client, outputs = self.make_controller()
        client.script("list-panes", CommandResult(True, (state_line(0),), None))
        client.script("capture-pane", CommandResult(True, ("prompt$",), None))
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: outputs)
        self.assertEqual(client.count("-E -1"), 0)
        self.assertEqual(client.count("capture-pane"), 1)
        self.assertIn(b"prompt$", b"".join(outputs))

    def test_first_attach_seeds_scrollback_from_tmux_history(self) -> None:
        controller, client, outputs = self.make_controller()
        client.script("list-panes", CommandResult(True, (state_line(120),), None))
        client.script("capture-pane", CommandResult(True, ("old output",), None))
        client.script("capture-pane", CommandResult(True, ("prompt$",), None))
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: outputs)
        data = b"".join(outputs)
        self.assertIn(b"old output", data)
        self.assertIn(b"prompt$", data)
        self.assertEqual(client.count("-S -120 -E -1"), 1)

    def test_failed_list_panes_is_retried_until_the_server_answers(self) -> None:
        controller, client, outputs = self.make_controller()
        client.script("list-panes", CommandResult(False, (), "timeout"))
        client.script("list-panes", CommandResult(True, (state_line(0),), None))
        client.script("capture-pane", CommandResult(True, ("prompt$",), None))
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: outputs, timeout=5.0)
        self.assertGreaterEqual(client.count("list-panes"), 2)
        self.assertIn(b"prompt$", b"".join(outputs))

    def test_failed_visible_capture_retries_setup_without_losing_history(self) -> None:
        controller, client, outputs = self.make_controller()
        client.script("list-panes", CommandResult(True, (state_line(50),), None))
        client.script("list-panes", CommandResult(True, (state_line(50),), None))
        client.script("capture-pane", CommandResult(True, ("old output",), None))
        client.script("capture-pane", CommandResult(False, (), "timeout"))
        client.script("capture-pane", CommandResult(True, ("old output",), None))
        client.script("capture-pane", CommandResult(True, ("screen",), None))
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: any(b"screen" in chunk for chunk in outputs), timeout=5.0)
        # History must be captured again on the retry: the first attempt
        # emitted nothing, so skipping history there would lose scrollback.
        self.assertEqual(client.count("-S -50 -E -1"), 2)
        merged = b"".join(outputs)
        self.assertIn(b"old output", merged)
        self.assertEqual(merged.count(b"old output"), 1)

    def test_input_sent_before_ready_is_flushed_after_replay(self) -> None:
        controller, client, outputs = self.make_controller()
        client.script("list-panes", CommandResult(True, (state_line(0),), None))
        client.script("capture-pane", CommandResult(True, ("prompt$",), None))
        controller.start(columns=80, rows=24)
        controller.send_bytes(b"hi")
        client.connect()
        wait_until(lambda: client.count("send-keys") == 1)
        self.assertEqual(client.count("send-keys -t %1 -H 68 69"), 1)

    def test_dead_pane_subscription_reports_death_once(self) -> None:
        dead: list[int] = []
        controller, client, outputs = self.make_controller(
            on_pane_dead=lambda: dead.append(1)
        )
        client.script("list-panes", CommandResult(True, (state_line(0),), None))
        client.script("capture-pane", CommandResult(True, ("prompt$",), None))
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: outputs)
        event = ControlEvent(
            "subscription-changed",
            "%subscription-changed td-pane-dead @0 %1 : 1",
            ("td-pane-dead", "@0", "%1", ":", "1"),
            None,
            None,
        )
        client.on_event(event)
        client.on_event(event)
        wait_until(lambda: dead)
        self.assertEqual(dead, [1])

    def test_stop_is_terminal_for_the_transport_client(self) -> None:
        controller, client, outputs = self.make_controller()
        controller.start(columns=80, rows=24)
        controller.stop()
        self.assertTrue(client.stopped)


if __name__ == "__main__":
    unittest.main()
