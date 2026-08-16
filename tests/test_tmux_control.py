"""Offline tests for :mod:`termdeck.tmux_control` using controllable binary transports."""

from __future__ import annotations

from collections import deque
import threading
import time
import unittest

from termdeck.tmux_control import ClientState, ControlModeClient


class FakeStdout:
    """A condition-backed readline source so tests exercise the real reader thread."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._lines: deque[bytes] = deque()
        self._eof = False

    def feed(self, line: str | bytes) -> None:
        data = line.encode("latin-1") if isinstance(line, str) else line
        if not data.endswith(b"\n"):
            data += b"\n"
        with self._condition:
            self._lines.append(data)
            self._condition.notify_all()

    def eof(self) -> None:
        with self._condition:
            self._eof = True
            self._condition.notify_all()

    def readline(self) -> bytes:
        with self._condition:
            while not self._lines and not self._eof:
                self._condition.wait()
            if self._lines:
                return self._lines.popleft()
            return b""


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None


class FakeTransport:
    def __init__(self) -> None:
        self.stdout = FakeStdout()
        self.stdin = FakeStdin()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.eof()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0 if self.terminated else None


def wait_until(predicate: object, timeout: float = 1.0) -> None:
    """Avoid fixed sleeps, keeping thread tests deterministic and quick."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition was not satisfied before timeout")


class ControlModeClientTests(unittest.TestCase):
    def make_client(
        self,
        transport: FakeTransport | None = None,
        *,
        transports: list[FakeTransport] | None = None,
        on_event: object | None = None,
        sleep: object | None = None,
        on_state_change: object | None = None,
    ) -> tuple[ControlModeClient, list[object], list[FakeTransport]]:
        available = deque(transports if transports is not None else [transport or FakeTransport()])
        spawned: list[FakeTransport] = []
        events: list[object] = []

        def spawn() -> FakeTransport:
            if not available:
                raise RuntimeError("no scripted transport")
            item = available.popleft()
            spawned.append(item)
            return item

        callback = on_event if on_event is not None else events.append
        client = ControlModeClient(
            spawn_transport=spawn,
            on_event=callback,  # type: ignore[arg-type]
            on_state_change=on_state_change,  # type: ignore[arg-type]
            sleep=sleep if sleep is not None else time.sleep,  # type: ignore[arg-type]
            reconnect_initial_seconds=0.01,
            reconnect_max_seconds=0.04,
        )
        return client, events, spawned

    def start_connected(
        self,
        client: ControlModeClient,
        transport: FakeTransport | None = None,
    ) -> None:
        client.start()
        wait_until(lambda: client.state is ClientState.CONNECTED)
        if transport is not None:
            # A real tmux control-mode server always opens the conversation
            # with one greeting %begin/%end block before any command replies.
            transport.stdout.feed("%begin 0 0 0")
            transport.stdout.feed("%end 0 0 0")

    def tearDown(self) -> None:
        # Each test explicitly stops its own client; this assertion catches a test
        # that would otherwise leave a daemon reader alive for later tests.
        for thread in threading.enumerate():
            if thread.name == "tmux-control-reader":
                self.fail("tmux reader leaked from test: " + thread.name)

    def test_parses_session_and_window_notifications(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client, transport)
        transcript = [
            "%sessions-changed",
            "%session-changed $3",
            "%window-add @2",
            "%window-close @2",
            "%window-renamed @3 dev",
            "%unlinked-window-add @4",
            "%layout-change @3 deadbeef",
            "%pane-mode-changed %5",
        ]
        for line in transcript:
            transport.stdout.feed(line)
        wait_until(lambda: len(events) == len(transcript))
        self.assertEqual(
            [event.kind for event in events],
            [
                "sessions-changed", "session-changed", "window-add", "window-close",
                "window-renamed", "unlinked-window-add", "layout-change", "pane-mode-changed",
            ],
        )
        self.assertEqual(events[4].args, ("@3", "dev"))
        self.assertEqual(events[6].raw, "%layout-change @3 deadbeef")
        client.stop()

    def test_output_decodes_octal_backslashes_and_latin1_bytes(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client, transport)
        transport.stdout.feed(r"%output %5 A\015B\\C\303\251")
        wait_until(lambda: len(events) == 1)
        event = events[0]
        self.assertEqual(event.kind, "output")
        self.assertEqual(event.pane_id, "%5")
        self.assertEqual(event.args, ("%5", r"A\015B\\C\303\251"))
        self.assertEqual(event.payload, "A\rB\\C\u00c3\u00a9")
        client.stop()

    def test_unknown_control_record_is_an_event_outside_a_reply(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client, transport)
        transport.stdout.feed("%unforeseen abc 7")
        wait_until(lambda: len(events) == 1)
        self.assertEqual(events[0].kind, "unknown")
        self.assertEqual(events[0].args, ("abc", "7"))
        client.stop()

    def test_exit_is_dispatched_before_clean_stop(self) -> None:
        transport = FakeTransport()
        holder: dict[str, ControlModeClient] = {}
        events: list[object] = []

        def on_event(event: object) -> None:
            events.append(event)
            if getattr(event, "kind", None) == "exit":
                holder["client"].stop()

        client, _, _ = self.make_client(transport, on_event=on_event)
        holder["client"] = client
        self.start_connected(client, transport)
        transport.stdout.feed("%exit server closed")
        wait_until(lambda: client.state is ClientState.STOPPED)
        self.assertEqual(events[0].kind, "exit")
        self.assertEqual(events[0].args, ("server", "closed"))

    def test_greeting_block_without_pending_command_is_swallowed(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client)
        transport.stdout.feed("%begin 1 0 0")
        transport.stdout.feed("greeting body")
        transport.stdout.feed("%end 1 0 0")
        time.sleep(0.02)
        self.assertEqual(events, [])
        client.stop()

    def test_command_enqueued_before_greeting_does_not_steal_greeting_block(self) -> None:
        # Regression: a command queued before the greeting block arrived used to
        # claim the greeting as its reply, shifting every later reply by one and
        # leaving panes permanently mute.
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        client.start()
        wait_until(lambda: client.state is ClientState.CONNECTED)
        box: list[object] = []
        worker = threading.Thread(target=lambda: box.append(client.send_command("list-panes")))
        worker.start()
        wait_until(lambda: transport.stdin.writes == [b"list-panes\n"])
        # Greeting arrives only after the command is already queued.
        transport.stdout.feed("%begin 1 0 0")
        transport.stdout.feed("greeting body")
        transport.stdout.feed("%end 1 0 0")
        transport.stdout.feed("%begin 2 1 0")
        transport.stdout.feed("pane state")
        transport.stdout.feed("%end 2 1 0")
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(box[0].ok)
        self.assertEqual(box[0].lines, ("pane state",))
        self.assertEqual(events, [])
        client.stop()

    def test_send_command_returns_reply_body(self) -> None:
        transport = FakeTransport()
        client, _, _ = self.make_client(transport)
        self.start_connected(client, transport)
        results: list[object] = []
        worker = threading.Thread(target=lambda: results.append(client.send_command("list-sessions")))
        worker.start()
        wait_until(lambda: transport.stdin.writes == [b"list-sessions\n"])
        transport.stdout.feed("%begin 2 1 0")
        transport.stdout.feed("alpha")
        transport.stdout.feed("beta")
        transport.stdout.feed("%end 2 1 0")
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].lines, ("alpha", "beta"))
        client.stop()

    def test_error_reply_returns_failed_result_and_body(self) -> None:
        transport = FakeTransport()
        client, _, _ = self.make_client(transport)
        self.start_connected(client, transport)
        results: list[object] = []
        worker = threading.Thread(target=lambda: results.append(client.send_command("bad-command")))
        worker.start()
        wait_until(lambda: bool(transport.stdin.writes))
        transport.stdout.feed("%begin 3 2 0")
        transport.stdout.feed("details")
        transport.stdout.feed("%error 3 2 denied")
        worker.join(1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].lines, ("details",))
        self.assertEqual(results[0].error, "3 2 denied")
        client.stop()

    def test_two_commands_match_replies_in_fifo_order(self) -> None:
        transport = FakeTransport()
        client, _, _ = self.make_client(transport)
        self.start_connected(client, transport)
        results: dict[str, object] = {}
        first = threading.Thread(target=lambda: results.setdefault("first", client.send_command("one")))
        second = threading.Thread(target=lambda: results.setdefault("second", client.send_command("two")))
        first.start()
        wait_until(lambda: transport.stdin.writes == [b"one\n"])
        second.start()
        wait_until(lambda: transport.stdin.writes == [b"one\n", b"two\n"])
        transport.stdout.feed("%begin 4 1 0")
        transport.stdout.feed("reply one")
        transport.stdout.feed("%end 4 1 0")
        transport.stdout.feed("%begin 4 2 0")
        transport.stdout.feed("reply two")
        transport.stdout.feed("%end 4 2 0")
        first.join(1)
        second.join(1)
        self.assertEqual(results["first"].lines, ("reply one",))
        self.assertEqual(results["second"].lines, ("reply two",))
        client.stop()

    def test_timeout_returns_timeout_result(self) -> None:
        transport = FakeTransport()
        client, _, _ = self.make_client(transport)
        self.start_connected(client, transport)
        result = client.send_command("never-replies", timeout=0.02)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")
        client.stop()

    def test_notification_interleaved_in_reply_is_dispatched_not_body(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client, transport)
        result_box: list[object] = []
        worker = threading.Thread(target=lambda: result_box.append(client.send_command("show")))
        worker.start()
        wait_until(lambda: bool(transport.stdin.writes))
        transport.stdout.feed("%begin 5 1 0")
        transport.stdout.feed("first")
        transport.stdout.feed("%window-add @9")
        transport.stdout.feed("last")
        transport.stdout.feed("%end 5 1 0")
        worker.join(1)
        self.assertEqual(result_box[0].lines, ("first", "last"))
        self.assertEqual([event.kind for event in events], ["window-add"])
        client.stop()

    def test_unknown_percent_line_inside_reply_is_preserved_as_body(self) -> None:
        transport = FakeTransport()
        client, events, _ = self.make_client(transport)
        self.start_connected(client, transport)
        box: list[object] = []
        worker = threading.Thread(target=lambda: box.append(client.send_command("show")))
        worker.start()
        wait_until(lambda: bool(transport.stdin.writes))
        transport.stdout.feed("%begin 6 1 0")
        transport.stdout.feed("%not-a-notification survives")
        transport.stdout.feed("%end 6 1 0")
        worker.join(1)
        self.assertEqual(box[0].lines, ("%not-a-notification survives",))
        self.assertEqual(events, [])
        client.stop()

    def test_eof_reconnects_with_injected_backoff(self) -> None:
        first = FakeTransport()
        second = FakeTransport()
        delays: list[float] = []
        client, _, spawned = self.make_client(transports=[first, second], sleep=delays.append)
        self.start_connected(client, first)
        first.stdout.eof()
        wait_until(lambda: len(spawned) == 2)
        self.assertEqual(client.reconnect_count, 1)
        self.assertEqual(delays, [0.01])
        self.assertIs(client.state, ClientState.CONNECTED)
        client.stop()

    def test_stop_during_injected_reconnect_halts_promptly(self) -> None:
        first = FakeTransport()
        holder: dict[str, ControlModeClient] = {}
        delays: list[float] = []

        def stop_while_waiting(seconds: float) -> None:
            delays.append(seconds)
            holder["client"].stop()

        client, _, _ = self.make_client(transports=[first], sleep=stop_while_waiting)
        holder["client"] = client
        self.start_connected(client, first)
        first.stdout.eof()
        wait_until(lambda: client.state is ClientState.STOPPED)
        thread = client._reader_thread
        self.assertEqual(delays, [0.01])
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())

    def test_stop_joins_the_reader_thread(self) -> None:
        transport = FakeTransport()
        client, _, _ = self.make_client(transport)
        self.start_connected(client, transport)
        client.stop()
        thread = client._reader_thread
        self.assertTrue(transport.terminated)
        self.assertIs(client.state, ClientState.STOPPED)
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())

    def test_repeated_start_is_harmless(self) -> None:
        transport = FakeTransport()
        client, _, spawned = self.make_client(transport)
        client.start()
        client.start()
        wait_until(lambda: client.state is ClientState.CONNECTED)
        self.assertEqual(spawned, [transport])
        client.stop()

    def test_validation_rejects_bad_limits(self) -> None:
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            ControlModeClient(
                spawn_transport=lambda: transport,
                on_event=lambda event: None,
                max_pending_commands=0,
            )


class TimeoutFramingTests(unittest.TestCase):
    """A timed-out command's late reply must not shift later replies."""

    def test_timed_out_reply_block_is_consumed_not_shifted(self) -> None:
        transport = FakeTransport()
        client = ControlModeClient(
            spawn_transport=lambda: transport,
            on_event=lambda event: None,
        )
        self.addCleanup(client.stop)
        client.start()
        wait_until(lambda: client.state is ClientState.CONNECTED)
        transport.stdout.feed("%begin 0 0 0")
        transport.stdout.feed("%end 0 0 0")

        timed_out = client.send_command("slow-command", timeout=0.05)
        self.assertFalse(timed_out.ok)
        self.assertEqual(timed_out.error, "timeout")

        results: list[object] = []
        waiter = threading.Thread(
            target=lambda: results.append(
                client.send_command("fast-command", timeout=2.0)
            ),
            daemon=True,
        )
        waiter.start()
        wait_until(
            lambda: any(b"fast-command" in write for write in transport.stdin.writes)
        )

        # The late reply for the timed-out command arrives first; the reader
        # must pair it with the abandoned FIFO entry and hand the NEXT block
        # to the still-waiting caller.
        transport.stdout.feed("%begin 1 1 0")
        transport.stdout.feed("SLOW-REPLY")
        transport.stdout.feed("%end 1 1 0")
        transport.stdout.feed("%begin 2 2 0")
        transport.stdout.feed("FAST-REPLY")
        transport.stdout.feed("%end 2 2 0")

        waiter.join(timeout=2.0)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.lines, ("FAST-REPLY",))


if __name__ == "__main__":
    unittest.main()
