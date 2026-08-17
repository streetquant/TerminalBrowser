"""Persistent, transport-agnostic client for tmux control mode.

The client intentionally has no UI dependency: callers decide how reader-thread
notifications are marshalled to their event loop.  Keeping protocol handling here
allows one connection to replace repeated, expensive tmux subprocess calls.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Any, Callable, Deque, Protocol


class ClientState(Enum):
    """Lifecycle states exposed so callers can present useful connection status."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class ControlEvent:
    """A notification emitted by tmux, kept raw as well as parsed for resilience."""

    kind: str
    raw: str
    args: tuple[str, ...]
    pane_id: str | None
    payload: str | None


@dataclass(frozen=True)
class CommandResult:
    """The complete reply for one command, preserving tmux body line boundaries."""

    ok: bool
    lines: tuple[str, ...]
    error: str | None


class _ReadableBinary(Protocol):
    def readline(self) -> bytes: ...


class _WritableBinary(Protocol):
    def write(self, data: bytes) -> Any: ...

    def flush(self) -> Any: ...


class _Transport(Protocol):
    stdout: _ReadableBinary
    stdin: _WritableBinary

    def terminate(self) -> Any: ...

    def wait(self, timeout: float | None = None) -> Any: ...

    def poll(self) -> Any: ...


@dataclass
class _PendingCommand:
    """Mutable reply accumulator owned by the reader until it signals its waiter."""

    done: threading.Event = field(default_factory=threading.Event)
    lines: list[str] = field(default_factory=list)
    result: CommandResult | None = None


# tmux's notification vocabulary is deliberately explicit.  Lines not in this
# set remain observable as ``unknown`` rather than accidentally being swallowed
# as output from a command reply.
_NOTIFICATION_KINDS = {
    "%client-detached": "client-detached",
    "%client-session-changed": "client-session-changed",
    "%config-error": "config-error",
    "%continue": "continue",
    "%exit": "exit",
    "%extended-output": "extended-output",
    "%layout-change": "layout-change",
    "%message": "message",
    "%output": "output",
    "%pane-add": "pane-add",
    "%pane-changed": "pane-changed",
    "%pane-exited": "pane-exited",
    "%pane-focus-in": "pane-focus-in",
    "%pane-focus-out": "pane-focus-out",
    "%pane-mode-changed": "pane-mode-changed",
    "%pane-offset": "pane-offset",
    "%pane-resized": "pane-resized",
    "%paste-buffer-changed": "paste-buffer-changed",
    "%session-changed": "session-changed",
    "%sessions-changed": "sessions-changed",
    "%subscription-changed": "subscription-changed",
    "%unlinked-window-add": "unlinked-window-add",
    "%unlinked-window-close": "unlinked-window-close",
    "%unlinked-window-renamed": "unlinked-window-renamed",
    "%window-add": "window-add",
    "%window-close": "window-close",
    "%window-pane-changed": "window-pane-changed",
    "%window-renamed": "window-renamed",
}


class ControlModeClient:
    """Maintain one reconnecting tmux ``-C`` transport and route its protocol.

    A single reader thread owns parsing because tmux replies are ordered.  Locks
    only protect shared lifecycle and pending-command state, so user callbacks
    never run while an internal lock is held and cannot deadlock the client.
    """

    def __init__(
        self,
        *,
        spawn_transport: Callable[[], _Transport],
        on_event: Callable[[ControlEvent], None],
        on_state_change: Callable[[ClientState], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        max_pending_commands: int = 64,
    ) -> None:
        """Configure the client without starting I/O, so setup remains testable.

        Backoff parameters are validated early because invalid values otherwise
        create a busy reconnect loop that is difficult for a GUI to diagnose.
        """
        if reconnect_initial_seconds < 0:
            raise ValueError("reconnect_initial_seconds must be non-negative")
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds must be >= reconnect_initial_seconds")
        if max_pending_commands <= 0:
            raise ValueError("max_pending_commands must be positive")

        self._spawn_transport = spawn_transport
        self._on_event = on_event
        self._on_state_change = on_state_change
        self._clock = clock
        self._sleep = sleep
        self._reconnect_initial_seconds = reconnect_initial_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._max_pending_commands = max_pending_commands

        self._lock = threading.RLock()
        self._started = False
        self._stopped = False
        self._state = ClientState.CONNECTING
        self._transport: _Transport | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._queued: Deque[_PendingCommand] = deque()
        self._active: _PendingCommand | None = None
        self._swallow_block = False
        self._greeting_pending = False
        self.reconnect_count = 0

    @property
    def state(self) -> ClientState:
        """Return a lock-consistent lifecycle snapshot for callers' status UI."""
        with self._lock:
            return self._state

    def start(self) -> None:
        """Start the sole reader once; repeated calls are harmless for UI callers."""
        with self._lock:
            if self._started or self._stopped:
                return
            self._started = True
            self._reader_thread = threading.Thread(
                target=self._reader_main,
                name="tmux-control-reader",
                daemon=True,
            )
            reader = self._reader_thread
        reader.start()

    def stop(self) -> None:
        """Wake blocking I/O and resolve waiters so application shutdown cannot hang."""
        with self._lock:
            if self._stopped:
                reader = self._reader_thread
                transport = None
            else:
                self._stopped = True
                self._stop_event.set()
                self._connected_event.set()
                transport = self._transport
                self._transport = None
                self._fail_all_locked("stopped")
                reader = self._reader_thread

        self._set_state(ClientState.STOPPED)
        if transport is not None:
            self._terminate_quietly(transport)
        if reader is not None and reader is not threading.current_thread():
            # A bounded join keeps a defective third-party transport from blocking
            # process shutdown while normal transports exit immediately on terminate.
            reader.join(timeout=2.0)

    def send_command(
        self, command: str, *, timeout: float | None = 5.0
    ) -> CommandResult:
        """Send one tmux command and wait for its FIFO-matched control reply.

        Commands are placed in the FIFO before bytes are written, preventing a
        fast local tmux from producing ``%begin`` before the reader can associate
        it with the correct caller.
        """
        if not isinstance(command, str):
            raise TypeError("command must be str")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")

        deadline = None if timeout is None else self._clock() + timeout
        pending = _PendingCommand()

        while True:
            with self._lock:
                if self._stopped:
                    return CommandResult(False, (), "stopped")
                transport = self._transport
                ready = transport is not None and self._state is ClientState.CONNECTED
                if ready:
                    if len(self._queued) + (1 if self._active is not None else 0) >= self._max_pending_commands:
                        return CommandResult(False, (), "too many pending commands")
                    self._queued.append(pending)
                    try:
                        transport.stdin.write((command + "\n").encode("utf-8"))
                        transport.stdin.flush()
                    except Exception as exc:
                        self._queued.remove(pending)
                        return CommandResult(False, (), "write failed: " + str(exc))
                    break
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                return CommandResult(False, (), "timeout")
            self._connected_event.wait(remaining)
            self._connected_event.clear()

        while not pending.done.is_set():
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                # The command's bytes were already written, so tmux will still
                # emit a reply block for it.  The pending entry must stay in
                # the FIFO so the reader consumes that block when it arrives;
                # removing it here would pair every later reply with the
                # wrong caller and permanently desync the connection.
                return CommandResult(False, (), "timeout")
            pending.done.wait(remaining)

        # The event is set only after result assignment in the reader/failure path.
        return pending.result or CommandResult(False, (), "disconnected")

    def _reader_main(self) -> None:
        """Own connection attempts and parsing so reply framing remains sequential."""
        backoff = self._reconnect_initial_seconds
        while not self._stop_event.is_set():
            try:
                transport = self._spawn_transport()
            except Exception:
                if not self._schedule_reconnect(backoff):
                    break
                backoff = min(self._reconnect_max_seconds, max(backoff * 2, backoff))
                continue

            with self._lock:
                if self._stopped:
                    self._terminate_quietly(transport)
                    break
                self._transport = transport
                self._swallow_block = False
                # tmux opens every control connection with one %begin/%end
                # block for the attach command itself. Claim it by position:
                # a caller can enqueue a command before the reader parses the
                # greeting, and letting that command steal the greeting block
                # shifts every later reply onto the wrong caller.
                self._greeting_pending = True
            self._connected_event.set()
            self._set_state(ClientState.CONNECTED)
            backoff = self._reconnect_initial_seconds

            disconnected = self._read_transport(transport)
            if not disconnected or self._stop_event.is_set():
                break

            with self._lock:
                if self._transport is transport:
                    self._transport = None
                    self._connected_event.clear()
                    self._fail_all_locked("disconnected")
            self._terminate_quietly(transport)
            if not self._schedule_reconnect(backoff):
                break
            backoff = min(self._reconnect_max_seconds, max(backoff * 2, backoff))

        if self._stop_event.is_set():
            self._set_state(ClientState.STOPPED)

    def _read_transport(self, transport: _Transport) -> bool:
        """Read until EOF or ``%exit``; both require a fresh control connection."""
        while not self._stop_event.is_set():
            try:
                data = transport.stdout.readline()
            except Exception:
                return True
            if not data:
                return True
            if isinstance(data, bytes):
                raw = data.decode("latin-1")
            else:
                raw = str(data)
            if raw.endswith("\n"):
                raw = raw[:-1]
                if raw.endswith("\r"):
                    raw = raw[:-1]
            if self._handle_line(raw):
                return True
        return False

    def _handle_line(self, raw: str) -> bool:
        """Parse one line, returning true only when tmux explicitly sent ``%exit``."""
        if raw.startswith("%begin") and (raw == "%begin" or raw[6:7].isspace()):
            with self._lock:
                if self._active is not None:
                    self._active.lines.append(raw)
                elif self._greeting_pending:
                    # The first block on a connection is always the attach
                    # greeting, never a queued command's reply.
                    self._greeting_pending = False
                    self._swallow_block = True
                elif not self._swallow_block and self._queued:
                    self._active = self._queued.popleft()
                else:
                    # A block with no waiting caller is protocol noise.
                    self._swallow_block = True
            return False

        if raw.startswith("%end") and (raw == "%end" or raw[4:5].isspace()):
            with self._lock:
                if self._active is not None:
                    pending = self._active
                    self._active = None
                    pending.result = CommandResult(True, tuple(pending.lines), None)
                    pending.done.set()
                else:
                    self._swallow_block = False
            return False

        if raw.startswith("%error") and (raw == "%error" or raw[6:7].isspace()):
            with self._lock:
                if self._active is not None:
                    pending = self._active
                    self._active = None
                    error = raw[6:].strip() or "tmux error"
                    pending.result = CommandResult(False, tuple(pending.lines), error)
                    pending.done.set()
                else:
                    self._swallow_block = False
            return False

        name = raw.partition(" ")[0] if raw.startswith("%") else ""
        if name in _NOTIFICATION_KINDS:
            event = self._notification_from_line(raw)
            # ``name`` was checked above, so this cannot be None.
            assert event is not None
            self._dispatch_event(event)
            return event.kind == "exit"

        with self._lock:
            if self._active is not None:
                self._active.lines.append(raw)
                return False
            if self._swallow_block:
                return False
        if raw.startswith("%"):
            self._dispatch_event(
                ControlEvent("unknown", raw, tuple(raw.split()[1:]), None, None)
            )
        return False

    def _notification_from_line(self, raw: str) -> ControlEvent | None:
        """Recognize notifications before treating percent-prefixed body text as data."""
        if not raw.startswith("%"):
            return None
        name, separator, remainder = raw.partition(" ")
        kind = _NOTIFICATION_KINDS.get(name)
        if kind is None:
            return None
        args = tuple(remainder.split()) if separator else ()
        if kind != "output":
            return ControlEvent(kind, raw, args, None, None)

        pane, payload_separator, encoded = remainder.partition(" ")
        pane_id = pane or None
        payload = self._decode_output(encoded) if payload_separator else ""
        return ControlEvent(kind, raw, args, pane_id, payload)

    def _dispatch_event(self, event: ControlEvent) -> None:
        """Isolate callback failures because one bad observer must not lose I/O."""
        try:
            self._on_event(event)
        except Exception:
            pass

    @staticmethod
    def _decode_output(encoded: str) -> str:
        """Decode tmux octal quoting without assuming output is valid UTF-8."""
        decoded: list[str] = []
        index = 0
        while index < len(encoded):
            char = encoded[index]
            if char != "\\" or index + 1 >= len(encoded):
                decoded.append(char)
                index += 1
                continue
            following = encoded[index + 1]
            octal = encoded[index + 1 : index + 4]
            if len(octal) == 3 and all("0" <= digit <= "7" for digit in octal):
                decoded.append(chr(int(octal, 8)))
                index += 4
            elif following == "\\":
                decoded.append("\\")
                index += 2
            else:
                # Preserve malformed/unknown quoting; losing terminal text is worse
                # than exposing tmux's literal sequence to a caller.
                decoded.append("\\")
                index += 1
        return "".join(decoded)

    def _schedule_reconnect(self, seconds: float) -> bool:
        """Record a retry before waiting, making reconnect telemetry deterministic."""
        with self._lock:
            if self._stopped:
                return False
            self.reconnect_count += 1
        self._set_state(ClientState.RECONNECTING)
        if self._sleep is time.sleep:
            # Event.wait is interruptible, unlike time.sleep, which matters when a
            # user closes the app during a long capped retry delay.
            self._stop_event.wait(seconds)
        else:
            self._sleep(seconds)
        return not self._stop_event.is_set()

    def _set_state(self, state: ClientState) -> None:
        """Notify outside the lock so state observers may safely call this client."""
        with self._lock:
            if self._stopped and state is not ClientState.STOPPED:
                return
            if self._state is state:
                return
            self._state = state
        if self._on_state_change is not None:
            try:
                self._on_state_change(state)
            except Exception:
                # UI observers must not take down the process-wide control channel.
                pass

    def _fail_all_locked(self, error: str) -> None:
        """Release every blocked sender when a connection can no longer reply."""
        if self._active is not None:
            self._active.result = CommandResult(False, tuple(self._active.lines), error)
            self._active.done.set()
            self._active = None
        while self._queued:
            pending = self._queued.popleft()
            pending.result = CommandResult(False, (), error)
            pending.done.set()

    def _remaining(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - self._clock())

    @staticmethod
    def _close_stream_quietly(stream: object | None) -> None:
        """Close one transport stream without letting teardown mask its cause."""

        close = getattr(stream, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            # A pipe can report BrokenPipe while being closed after its child
            # exits. It is still closed from this process's ownership model.
            pass

    @staticmethod
    def _poll_quietly(transport: _Transport) -> object | None:
        try:
            return transport.poll()
        except Exception:
            return None

    @classmethod
    def _terminate_quietly(cls, transport: _Transport) -> None:
        """Close, terminate, reap, and finally release one control transport.

        Merely calling ``terminate`` leaves ``Popen`` pipe objects to be closed
        by interpreter finalization.  That produced noisy ``BrokenPipeError``
        traces and can retain zombie children during repeated GUI teardown.
        The write side closes first, then TERM receives a bounded grace period;
        a supported ``kill`` method is the final fallback.  Every stream is
        closed in ``finally`` so this routine remains idempotent under stop/EOF
        races between the owner and reader threads.
        """

        stdin = getattr(transport, "stdin", None)
        stdout = getattr(transport, "stdout", None)
        stderr = getattr(transport, "stderr", None)
        cls._close_stream_quietly(stdin)
        try:
            if cls._poll_quietly(transport) is None:
                try:
                    transport.terminate()
                except Exception:
                    pass
            try:
                transport.wait(timeout=0.75)
            except Exception:
                if cls._poll_quietly(transport) is None:
                    kill = getattr(transport, "kill", None)
                    if callable(kill):
                        try:
                            kill()
                        except Exception:
                            pass
                try:
                    transport.wait(timeout=0.75)
                except Exception:
                    pass
        finally:
            cls._close_stream_quietly(stdout)
            cls._close_stream_quietly(stderr)
            cls._close_stream_quietly(stdin)
