"""Offline tests for :mod:`termdeck.pane_stream` with an injected fake control client.

The fake client answers commands synchronously from a one-shot prefix script,
so these tests exercise the controller's real worker thread, ready gate,
replay assembly, and setup retry logic without any tmux server.
"""

from __future__ import annotations

import pathlib
import signal
import sys
import tempfile
import threading
import time
import unittest

from termdeck.pane_stream import (
    PaneStreamController,
    StreamSetupGate,
    _STATE_FIELDS,
    spawn_control_transport,
    terminate_orphan_control_clients,
)
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


class StreamSetupGateTests(unittest.TestCase):
    def test_limit_bounds_concurrent_holders(self) -> None:
        gate = StreamSetupGate(limit=1)
        self.assertTrue(gate.acquire(object()))
        acquired = threading.Event()

        def worker() -> None:
            gate.acquire(object())
            acquired.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.15)
        self.assertFalse(acquired.is_set())
        gate.release()
        wait_until(acquired.is_set)
        gate.release()

    def test_promote_moves_waiting_token_ahead(self) -> None:
        gate = StreamSetupGate(limit=1)
        self.assertTrue(gate.acquire(object()))
        order: list[str] = []

        def worker(name: str, token: object) -> None:
            gate.acquire(token)
            order.append(name)
            gate.release()

        first_token = object()
        second_token = object()
        threading.Thread(
            target=worker, args=("first", first_token), daemon=True
        ).start()
        wait_until(lambda: len(gate._waiting) == 1)
        threading.Thread(
            target=worker, args=("second", second_token), daemon=True
        ).start()
        wait_until(lambda: len(gate._waiting) == 2)
        gate.promote(second_token)
        gate.release()
        wait_until(lambda: len(order) == 2)
        self.assertEqual(order, ["second", "first"])

    def test_abort_while_waiting_returns_false(self) -> None:
        gate = StreamSetupGate(limit=1)
        self.assertTrue(gate.acquire(object()))
        abort = threading.Event()
        results: list[bool] = []

        def worker() -> None:
            results.append(gate.acquire(object(), should_abort=abort.is_set))

        threading.Thread(target=worker, daemon=True).start()
        abort.set()
        wait_until(lambda: bool(results))
        self.assertEqual(results, [False])
        gate.release()


class ControllerGateIntegrationTests(unittest.TestCase):
    def make_gated_controller(self, gate, tag, order):
        created: dict[str, FakeControlClient] = {}

        def factory(**factory_kwargs) -> FakeControlClient:
            client = FakeControlClient(**factory_kwargs)
            created["client"] = client
            return client

        controller = PaneStreamController(
            attach_argv=["tmux", "attach-session", "-t", tag],
            on_output=lambda data, tag=tag: order.append(tag),
            client_factory=factory,
            setup_gate=gate,
        )
        self.addCleanup(controller.stop)
        client = created["client"]
        client.script("list-panes", CommandResult(True, (state_line(0),), None))
        client.script("capture-pane", CommandResult(True, ("ready$",), None))
        return controller, client

    def test_setup_waits_for_a_free_gate_slot(self) -> None:
        gate = StreamSetupGate(limit=1)
        self.assertTrue(gate.acquire(object()))
        order: list[str] = []
        controller, client = self.make_gated_controller(gate, "a", order)
        controller.start(columns=80, rows=24)
        client.connect()
        time.sleep(0.2)
        self.assertEqual(client.count("list-panes"), 0)
        gate.release()
        wait_until(lambda: order)
        self.assertEqual(client.count("list-panes"), 1)

    def test_slot_is_released_after_setup_finishes(self) -> None:
        gate = StreamSetupGate(limit=1)
        order: list[str] = []
        controller, client = self.make_gated_controller(gate, "a", order)
        controller.start(columns=80, rows=24)
        client.connect()
        wait_until(lambda: order)
        wait_until(lambda: gate._active == 0)

    def test_prioritize_setup_jumps_the_queue(self) -> None:
        gate = StreamSetupGate(limit=1)
        self.assertTrue(gate.acquire(object()))
        order: list[str] = []
        background, background_client = self.make_gated_controller(
            gate, "background", order
        )
        visible, visible_client = self.make_gated_controller(
            gate, "visible", order
        )
        background.start(columns=80, rows=24)
        background_client.connect()
        wait_until(lambda: len(gate._waiting) == 1)
        visible.start(columns=80, rows=24)
        visible_client.connect()
        wait_until(lambda: len(gate._waiting) == 2)
        visible.prioritize_setup()
        gate.release()
        wait_until(lambda: len(order) >= 2)
        self.assertEqual(order[0], "visible")


class OrphanControlClientSweepTests(unittest.TestCase):
    def _add_process(self, root, pid, argv, ppid, comm="tmux") -> None:
        directory = root / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(b"\x00".join(argv) + b"\x00")
        (directory / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194304 0 0 0 0",
            encoding="latin-1",
        )

    def test_only_orphaned_prefixed_clients_are_signalled(self) -> None:
        root = pathlib.Path(tempfile.mkdtemp())
        control = [b"/usr/bin/tmux", b"-C", b"attach-session", b"-t"]
        self._add_process(root, 101, control + [b"termdeck_programs_1"], ppid=1)
        self._add_process(root, 102, control + [b"termdeck_programs_2"], ppid=4242)
        self._add_process(
            root,
            103,
            [b"/usr/bin/tmux", b"attach-session", b"-t", b"termdeck_x", b"-f"],
            ppid=1,
        )
        self._add_process(root, 104, control + [b"someone_elses_session"], ppid=1)
        self._add_process(
            root,
            105,
            [b"/usr/bin/nvim", b"-C", b"attach-session", b"-t", b"termdeck_y"],
            ppid=1,
            comm="nvim",
        )
        killed: list[tuple[int, int]] = []
        count = terminate_orphan_control_clients(
            proc_root=str(root),
            kill=lambda pid, sig: killed.append((pid, sig)),
        )
        self.assertEqual(killed, [(101, signal.SIGTERM)])
        self.assertEqual(count, 1)

    def test_missing_proc_root_is_harmless(self) -> None:
        count = terminate_orphan_control_clients(
            proc_root="/nonexistent/proc-root",
            kill=lambda pid, sig: (_ for _ in ()).throw(AssertionError("no kill")),
        )
        self.assertEqual(count, 0)

    def test_failed_kill_is_not_counted(self) -> None:
        root = pathlib.Path(tempfile.mkdtemp())
        self._add_process(
            root,
            201,
            [b"/usr/bin/tmux", b"-C", b"attach-session", b"-t", b"termdeck_z"],
            ppid=1,
        )

        def failing_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError(pid)

        count = terminate_orphan_control_clients(
            proc_root=str(root), kill=failing_kill
        )
        self.assertEqual(count, 0)


class TransportParentDeathTests(unittest.TestCase):
    def test_spawned_child_has_the_parent_death_signal_set(self) -> None:
        probe = (
            "import ctypes, signal, sys; "
            "value = ctypes.c_int(0); "
            "ctypes.CDLL(None, use_errno=True).prctl(2, ctypes.byref(value), 0, 0, 0); "
            "sys.exit(0 if value.value == int(signal.SIGTERM) else 3)"
        )
        process = spawn_control_transport([sys.executable, "-c", probe], None, None)
        try:
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
