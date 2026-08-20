"""Stream one tmux pane's raw bytes into a local terminal emulator.

TermDeck renders terminals with VTE as the *only* terminal emulator.  tmux is
reduced to the persistence daemon it is good at being: this controller
attaches to one session in control mode (``tmux -C attach-session``), replays
the pane's screen once, then forwards the pane's verbatim byte stream
(``%output``) to the widget and the widget's input bytes back to the pane
(``send-keys -H``).  Because the pane's raw escape stream — including
alternate-screen switches — reaches VTE unmodified, full-screen applications
run on VTE's real alternate screen and there is no second client-side screen
model to diverge from.  Resizes are communicated with ``refresh-client -C``;
no client PTY exists at all.

The module is deliberately GTK-free.  Callbacks fire on internal threads and
callers marshal them onto their event loop.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping

from .tmux_control import ClientState, ControlEvent, ControlModeClient

# Bytes per send-keys command. Chunking keeps each control-mode command line
# short while remaining far faster than interactive typing rates.
_INPUT_CHUNK_BYTES = 64

# A transport may connect while tmux is busy replaying a large registry. Bound
# every pre-ready collection so a wedged server or an accidental paste cannot
# turn one tab into unbounded process memory. Output overflow retries from a
# fresh authoritative capture; input overflow rejects the new atomic payload.
_MAX_PENDING_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_PENDING_OUTPUT_CHUNKS = 4096
_MAX_PENDING_INPUT_BYTES = 1024 * 1024
_MAX_PENDING_INPUT_CHUNKS = 2048

# Modes replayed from tmux pane state on (re)attach so VTE starts from the
# same emulator state the application last negotiated. tmux reports these
# ``*_flag`` fields as 1 when enabled and 0 when disabled. Emit both sides of
# every transition: a fresh/reconnected VTE may otherwise retain a stale mode.
# Inverting the mouse flags makes an ordinary shell capture wheel input instead
# of scrolling native history, which is indistinguishable from a broken pane.
_MODE_SEQUENCES = (
    ("cursor_flag", "1", b"\x1b[?25h", b"\x1b[?25l"),
    ("keypad_cursor_flag", "1", b"\x1b[?1h", b"\x1b[?1l"),
    ("wrap_flag", "1", b"\x1b[?7h", b"\x1b[?7l"),
    ("insert_flag", "1", b"\x1b[4h", b"\x1b[4l"),
    ("origin_flag", "1", b"\x1b[?6h", b"\x1b[?6l"),
    ("mouse_standard_flag", "1", b"\x1b[?1000h", b"\x1b[?1000l"),
    ("mouse_button_flag", "1", b"\x1b[?1002h", b"\x1b[?1002l"),
    ("mouse_all_flag", "1", b"\x1b[?1003h", b"\x1b[?1003l"),
    ("mouse_sgr_flag", "1", b"\x1b[?1006h", b"\x1b[?1006l"),
)

_PANE_STATE_FORMAT = (
    "#{pane_id} #{pane_pid} #{alternate_on} #{pane_dead} #{cursor_x} #{cursor_y}"
    " #{cursor_flag} #{keypad_cursor_flag} #{wrap_flag} #{insert_flag}"
    " #{origin_flag} #{mouse_standard_flag} #{mouse_button_flag}"
    " #{mouse_all_flag} #{mouse_sgr_flag} #{history_size}"
)

_STATE_FIELDS = (
    "pane_id",
    "pane_pid",
    "alternate_on",
    "pane_dead",
    "cursor_x",
    "cursor_y",
    "cursor_flag",
    "keypad_cursor_flag",
    "wrap_flag",
    "insert_flag",
    "origin_flag",
    "mouse_standard_flag",
    "mouse_button_flag",
    "mouse_all_flag",
    "mouse_sgr_flag",
    "history_size",
)

_DEAD_SUBSCRIPTION = "td-pane-dead"


def control_attach_argv(attach_argv: list[str]) -> list[str]:
    """Turn a plain ``tmux attach-session`` argv into its control-mode form."""

    if not attach_argv:
        raise ValueError("attach_argv must not be empty")
    return [attach_argv[0], "-C", *attach_argv[1:]]


def attach_target(attach_argv: list[str]) -> str | None:
    """Extract the ``-t`` session target from an attach argv, if present."""

    for index, token in enumerate(attach_argv):
        if token == "-t" and index + 1 < len(attach_argv):
            return attach_argv[index + 1]
    return None


_PR_SET_PDEATHSIG = 1


def bind_transport_to_parent_death() -> None:
    """Runs in the forked child: SIGTERM this transport when its parent dies.

    Control clients otherwise survive a crashed or SIGKILLed instance, stay
    attached to the shared tmux server forever, and every restart stacks a
    new generation of zombie clients on top until the server drowns in
    them.
    """

    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
    except Exception:
        # Best effort: without the death signal the transport merely behaves
        # as before; the startup orphan sweep remains as the fallback.
        pass


def spawn_control_transport(
    argv: list[str],
    environment: Mapping[str, str] | None,
    cwd: str | None,
) -> subprocess.Popen[bytes]:
    """Spawn the ``tmux -C`` client used as the control-mode transport.

    Stderr is discarded rather than merged: tmux writes protocol only to
    stdout, and stray diagnostics must never corrupt %output framing.
    """

    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        preexec_fn=bind_transport_to_parent_death,
    )


# Concurrent attach setups allowed per process.  Every setup issues a
# refresh, a list-panes, and up to two capture-pane commands against the
# single-threaded tmux server; running every tab at once forms a convoy in
# which each command exceeds its timeout and the retries multiply the load.
# A small window keeps enough overlap to hide round-trip latency while the
# queue drains strictly in (priority, arrival) order.
_SETUP_CONCURRENCY = 6


class StreamSetupGate:
    """Rank and bound the attach setups running against one tmux server.

    ``acquire`` blocks its caller (a controller worker thread) until the
    token reaches the head of the queue and a slot is free; ``promote``
    moves the tab the user is looking at to the head so a visible terminal
    never waits behind dozens of background tabs.
    """

    def __init__(self, limit: int = _SETUP_CONCURRENCY) -> None:
        self._condition = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0
        self._waiting: deque[object] = deque()

    def acquire(
        self,
        token: object,
        *,
        priority: bool = False,
        should_abort: Callable[[], bool] | None = None,
    ) -> bool:
        """Wait for a setup slot; ``False`` when the caller aborted the wait."""

        with self._condition:
            if priority:
                self._waiting.appendleft(token)
            else:
                self._waiting.append(token)
            while True:
                if should_abort is not None and should_abort():
                    try:
                        self._waiting.remove(token)
                    except ValueError:
                        pass
                    self._condition.notify_all()
                    return False
                if self._active < self._limit and self._waiting[0] is token:
                    self._waiting.popleft()
                    self._active += 1
                    return True
                # Bounded wait so an abort (stop or reconnect) is noticed
                # promptly even without a matching notify.
                self._condition.wait(0.25)

    def release(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def promote(self, token: object) -> None:
        """Move a waiting token to the queue head; unknown tokens are ignored."""

        with self._condition:
            try:
                self._waiting.remove(token)
            except ValueError:
                return
            self._waiting.appendleft(token)
            self._condition.notify_all()


_DEFAULT_SETUP_GATE = StreamSetupGate()


def terminate_orphan_control_clients(
    session_prefix: str = "termdeck_",
    *,
    proc_root: str = "/proc",
    kill: Callable[[int, int], None] = os.kill,
) -> int:
    """SIGTERM ``tmux -C attach-session`` clients inherited by PID 1.

    A control-mode client is only a viewer: killing it never touches the
    tmux session, its pane, or the process running inside.  Candidates must
    be reparented to init (their spawning instance is dead) and target a
    session with our prefix, so live instances and user processes are never
    affected.  Returns the number of clients signalled.
    """

    reaped = 0
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(
                os.path.join(proc_root, entry, "cmdline"), "rb"
            ) as handle:
                argv = [part for part in handle.read().split(b"\x00") if part]
            with open(
                os.path.join(proc_root, entry, "stat"),
                "r",
                encoding="latin-1",
            ) as handle:
                stat_line = handle.read()
        except OSError:
            continue
        if len(argv) < 5:
            continue
        executable = os.path.basename(argv[0].decode("latin-1", "replace"))
        if executable != "tmux":
            continue
        arguments = [part.decode("latin-1", "replace") for part in argv[1:]]
        if "-C" not in arguments or "attach-session" not in arguments:
            continue
        target = None
        for index, word in enumerate(arguments):
            if word == "-t" and index + 1 < len(arguments):
                target = arguments[index + 1]
        if target is None or not target.startswith(session_prefix):
            continue
        # The parent PID is the fourth stat field; split after the closing
        # paren so an unusual executable name cannot shift fields.
        try:
            ppid = int(stat_line.rsplit(") ", 1)[1].split()[1])
        except (IndexError, ValueError):
            continue
        if ppid != 1:
            continue
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            continue
        reaped += 1
    return reaped


class ResizeSignalTicket:
    """One bounded completion barrier for setup-critical notification."""

    __slots__ = (
        "_event",
        "_lock",
        "_completed",
        "_succeeded",
        "_cancelled",
        "_error",
    )

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._completed = False
        self._succeeded = False
        self._cancelled = False
        self._error = ""

    def _finish(self, *, succeeded: bool, error: str = "") -> None:
        with self._lock:
            if self._completed:
                return
            self._completed = True
            self._succeeded = bool(succeeded)
            self._error = str(error or "")[-512:]
        self._event.set()

    def wait(self, timeout: float) -> bool:
        """Wait a finite interval and report whether work was processed.

        Completion and callback success are intentionally separate. Setup must
        wait until the notification attempt finishes before authoritative
        capture, but an optional callback exception must not strand a healthy
        shell or non-Codex pane.
        """

        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return False
        if timeout < 0:
            return False
        return bool(self._event.wait(timeout))

    def cancel(self, reason: str = "barrier cancelled") -> bool:
        """Cancel not-yet-started work and release the waiter exactly once."""

        with self._lock:
            if self._completed:
                return False
            self._cancelled = True
            self._completed = True
            self._succeeded = False
            self._error = str(reason or "barrier cancelled")[-512:]
        self._event.set()
        return True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def completed(self) -> bool:
        return self._event.is_set()

    @property
    def succeeded(self) -> bool:
        with self._lock:
            return self._completed and self._succeeded

    @property
    def error(self) -> str:
        with self._lock:
            return self._error


@dataclass(frozen=True)
class _ResizeSignalWork:
    key: tuple[object, ...]
    callback: Callable[[int, int, int], object]
    pane_root_pid: int
    columns: int
    rows: int
    ticket: ResizeSignalTicket | None


class ResizeSignalDispatcher:
    """One bounded, coalescing worker for containment resize notification.

    Ordinary resize traffic uses latest-value coalescing per callback and pane
    root. Setup repair uses unique tickets so nudge and final notifications
    cannot be merged across the replay boundary. Only redaction-safe status is
    retained; callback objects, return values, exception messages, and private
    launch data never enter history.
    """

    def __init__(
        self,
        *,
        max_pending: int = 256,
        history_limit: int = 64,
        poll_seconds: float = 0.05,
    ) -> None:
        max_pending = int(max_pending)
        history_limit = int(history_limit)
        poll_seconds = float(poll_seconds)
        if not 1 <= max_pending <= 4096:
            raise ValueError("max_pending must be in [1, 4096]")
        if not 1 <= history_limit <= 4096:
            raise ValueError("history_limit must be in [1, 4096]")
        if not 0.001 <= poll_seconds <= 1.0:
            raise ValueError("poll_seconds must be in [0.001, 1.0]")
        self._max_pending = max_pending
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._queue: queue.PriorityQueue[
            tuple[int, int, tuple[object, ...]]
        ] = queue.PriorityQueue(maxsize=max_pending)
        self._pending: dict[tuple[object, ...], _ResizeSignalWork] = {}
        self._history: deque[dict[str, object]] = deque(maxlen=history_limit)
        self._thread: threading.Thread | None = None
        self._active: _ResizeSignalWork | None = None
        self._stopped = False
        self._sequence = 0
        self._queue_sequence = 0

    def _record_locked(
        self,
        work: _ResizeSignalWork,
        *,
        status: str,
        error_type: str = "",
    ) -> None:
        self._history.append(
            {
                "pane_root_pid": work.pane_root_pid,
                "columns": work.columns,
                "rows": work.rows,
                "barrier": work.ticket is not None,
                "status": str(status)[:64],
                "error_type": str(error_type)[:128],
            }
        )

    def _ensure_worker_locked(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        if self._stopped:
            raise RuntimeError("resize signal dispatcher is stopped")
        thread = threading.Thread(
            target=self._worker_main,
            name="termdeck-resize-signal",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _enqueue(self, work: _ResizeSignalWork, *, coalesce: bool) -> bool:
        with self._lock:
            if self._stopped:
                if work.ticket is not None:
                    work.ticket._finish(
                        succeeded=False,
                        error="dispatcher shutdown",
                    )
                self._record_locked(work, status="rejected", error_type="stopped")
                return False
            if coalesce and work.key in self._pending:
                self._pending[work.key] = work
                self._record_locked(work, status="coalesced")
                return True
            if len(self._pending) >= self._max_pending:
                if work.ticket is not None:
                    work.ticket._finish(
                        succeeded=False,
                        error="dispatcher capacity exhausted",
                    )
                self._record_locked(work, status="rejected", error_type="capacity")
                return False
            self._pending[work.key] = work
            try:
                # Setup barriers outrank ordinary resize notifications so a
                # burst of background resizes cannot delay authoritative
                # replay behind the entire latest-value backlog.
                self._queue_sequence += 1
                priority = 0 if work.ticket is not None else 1
                self._queue.put_nowait(
                    (priority, self._queue_sequence, work.key)
                )
                self._ensure_worker_locked()
            except Exception as exc:
                if self._pending.get(work.key) is work:
                    self._pending.pop(work.key, None)
                error_type = type(exc).__name__
                if work.ticket is not None:
                    work.ticket._finish(
                        succeeded=False,
                        error=f"{error_type}: dispatcher start failed",
                    )
                self._record_locked(
                    work,
                    status="rejected",
                    error_type=error_type,
                )
                return False
            self._record_locked(work, status="queued")
            return True

    def submit_latest(
        self,
        callback: Callable[[int, int, int], object],
        *,
        pane_root_pid: int,
        columns: int,
        rows: int,
    ) -> bool:
        """Submit one nonblocking latest-value notification."""

        if not callable(callback):
            return False
        pane_root_pid = int(pane_root_pid)
        columns = int(columns)
        rows = int(rows)
        if pane_root_pid <= 1 or columns < 2 or rows < 2:
            return False
        key = ("latest", id(callback), pane_root_pid)
        return self._enqueue(
            _ResizeSignalWork(
                key,
                callback,
                pane_root_pid,
                columns,
                rows,
                None,
            ),
            coalesce=True,
        )

    def submit_barrier(
        self,
        callback: Callable[[int, int, int], object],
        *,
        pane_root_pid: int,
        columns: int,
        rows: int,
    ) -> ResizeSignalTicket | None:
        """Submit one non-coalesced setup barrier and return its ticket."""

        if not callable(callback):
            return None
        pane_root_pid = int(pane_root_pid)
        columns = int(columns)
        rows = int(rows)
        if pane_root_pid <= 1 or columns < 2 or rows < 2:
            return None
        ticket = ResizeSignalTicket()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        key = ("barrier", sequence)
        work = _ResizeSignalWork(
            key,
            callback,
            pane_root_pid,
            columns,
            rows,
            ticket,
        )
        return ticket if self._enqueue(work, coalesce=False) else None

    def _worker_main(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
            try:
                _priority, _sequence, key = self._queue.get(
                    timeout=self._poll_seconds
                )
            except queue.Empty:
                continue
            work: _ResizeSignalWork | None = None
            try:
                with self._lock:
                    work = self._pending.pop(key, None)
                    self._active = work
                    stopped = self._stopped
                if work is None:
                    continue
                if stopped:
                    if work.ticket is not None:
                        work.ticket.cancel("dispatcher shutdown")
                    continue
                if work.ticket is not None and work.ticket.cancelled:
                    with self._lock:
                        self._record_locked(work, status="cancelled")
                    continue
                try:
                    work.callback(
                        work.pane_root_pid,
                        work.columns,
                        work.rows,
                    )
                except Exception as exc:
                    error_type = type(exc).__name__
                    if work.ticket is not None:
                        work.ticket._finish(
                            succeeded=False,
                            error=f"{error_type}: callback failed",
                        )
                    with self._lock:
                        self._record_locked(
                            work,
                            status="failed",
                            error_type=error_type,
                        )
                else:
                    if work.ticket is not None:
                        work.ticket._finish(succeeded=True)
                    with self._lock:
                        self._record_locked(work, status="completed")
            finally:
                with self._lock:
                    self._active = None
                self._queue.task_done()

    def snapshot(self) -> dict[str, object]:
        """Return bounded status without callbacks or private values."""

        with self._lock:
            thread = self._thread
            return {
                "stopped": self._stopped,
                "thread_alive": bool(thread is not None and thread.is_alive()),
                "pending": len(self._pending),
                "active": self._active is not None,
                "history": list(self._history),
            }

    def shutdown(self, timeout: float = 1.0) -> bool:
        """Stop admission and release all pending ticket holders finitely."""

        try:
            timeout = max(0.0, float(timeout))
        except (TypeError, ValueError):
            timeout = 0.0
        with self._lock:
            self._stopped = True
            pending = list(self._pending.values())
            self._pending.clear()
            active = self._active
            thread = self._thread
        for work in [*pending, *([active] if active is not None else [])]:
            if work.ticket is not None:
                work.ticket.cancel("dispatcher shutdown")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return bool(thread is None or not thread.is_alive())


_DEFAULT_RESIZE_SIGNAL_DISPATCHER = ResizeSignalDispatcher()


class PaneStreamController:
    """Own one control-mode attachment and its pane's bidirectional stream.

    Lifecycle: ``start()`` spawns the transport; on every (re)connection the
    worker replays pane state and screen content through ``on_output`` before
    live ``%output`` bytes flow.  ``stop()`` is terminal.  All callbacks fire
    on internal threads.
    """

    def __init__(
        self,
        *,
        attach_argv: list[str],
        environment: Mapping[str, str] | None = None,
        cwd: str | None = None,
        on_output: Callable[[bytes], None],
        on_pane_dead: Callable[[], None] | None = None,
        on_state_change: Callable[[ClientState], None] | None = None,
        on_resize_applied: Callable[[int, int, int], object] | None = None,
        resize_signal_dispatcher: ResizeSignalDispatcher | None = None,
        resize_signal_timeout: float | None = None,
        repair_on_setup: bool = True,
        history_lines: int = 2000,
        command_timeout: float = 5.0,
        client_factory: Callable[..., ControlModeClient] | None = None,
        setup_gate: StreamSetupGate | None = None,
    ) -> None:
        self._attach_argv = list(attach_argv)
        self._target = attach_target(self._attach_argv)
        self._environment = dict(environment) if environment is not None else None
        self._cwd = cwd
        self._on_output = on_output
        self._on_pane_dead = on_pane_dead
        self._on_state_change = on_state_change
        self._on_resize_applied = on_resize_applied
        self._resize_signal_dispatcher = (
            resize_signal_dispatcher
            if resize_signal_dispatcher is not None
            else _DEFAULT_RESIZE_SIGNAL_DISPATCHER
        )
        self._repair_on_setup = bool(repair_on_setup)
        self._history_lines = max(0, int(history_lines))
        self._command_timeout = float(command_timeout)
        self._resize_signal_timeout = min(
            30.0,
            max(
                0.05,
                float(
                    self._command_timeout
                    if resize_signal_timeout is None
                    else resize_signal_timeout
                ),
            ),
        )

        self._lock = threading.Lock()
        self._tasks: queue.Queue[tuple[str, object] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopped = False
        self._started = False
        self._generation = 0
        self._ready = False
        self._pane_id: str | None = None
        self._pane_root_pid: int | None = None
        self._pane_dead_reported = False
        self._replayed_history = False
        self._pending_output: list[bytes] = []
        self._pending_output_bytes = 0
        self._pending_output_overflowed = False
        self._pending_input: list[bytes] = []
        self._unsent_input_bytes = 0
        self._unsent_input_chunks = 0
        self._size = (80, 24)
        self._resize_queued = False
        self._setup_attempts = 0
        self._setup_retry_timer: threading.Timer | None = None
        self._setup_retry_generation: int | None = None
        self._setup_gate = (
            setup_gate if setup_gate is not None else _DEFAULT_SETUP_GATE
        )
        self._setup_token: object = object()
        self._setup_priority = False

        factory = client_factory or ControlModeClient
        self._client = factory(
            spawn_transport=self._spawn_transport,
            on_event=self._handle_event,
            on_state_change=self._handle_state_change,
            reconnect_initial_seconds=0.25,
            reconnect_max_seconds=4.0,
        )

    # ------------------------------------------------------------- lifecycle

    @property
    def state(self) -> ClientState:
        return self._client.state

    @property
    def pane_id(self) -> str | None:
        with self._lock:
            return self._pane_id

    def _take_setup_retry_locked(self) -> threading.Timer | None:
        timer = self._setup_retry_timer
        self._setup_retry_timer = None
        self._setup_retry_generation = None
        return timer

    def start(self, *, columns: int, rows: int) -> None:
        """Begin streaming with the widget's current grid as the client size."""

        with self._lock:
            if self._started or self._stopped:
                return
            self._started = True
            self._size = (max(2, int(columns)), max(2, int(rows)))
        self._worker = threading.Thread(
            target=self._worker_main,
            name="pane-stream-worker",
            daemon=True,
        )
        self._worker.start()
        self._client.start()

    def stop(self) -> None:
        """Terminal shutdown; a stopped controller is never restarted."""

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._ready = False
            retry_timer = self._take_setup_retry_locked()
            self._pending_output.clear()
            self._pending_output_bytes = 0
            self._pending_output_overflowed = False
            self._pending_input.clear()
            self._unsent_input_bytes = 0
            self._unsent_input_chunks = 0
        if retry_timer is not None:
            retry_timer.cancel()
        self._client.stop()
        self._tasks.put(None)
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    # ------------------------------------------------------------------ I/O

    def send_bytes(self, data: bytes) -> bool:
        """Queue one atomic input payload, returning false at the bounded limit.

        Existing accepted input is never truncated to make room for a newer
        paste. The caller can therefore report the rejected payload honestly
        instead of replaying an ambiguous suffix after setup completes.
        """

        if not data:
            return True
        payload = bytes(data)
        with self._lock:
            if self._stopped:
                return False
            if (
                self._unsent_input_bytes + len(payload) > _MAX_PENDING_INPUT_BYTES
                or self._unsent_input_chunks >= _MAX_PENDING_INPUT_CHUNKS
            ):
                return False
            self._unsent_input_bytes += len(payload)
            self._unsent_input_chunks += 1
        self._tasks.put(("input", payload))
        return True

    def resize(self, *, columns: int, rows: int) -> None:
        """Coalesce grid changes; only the most recent size is applied."""

        columns = max(2, int(columns))
        rows = max(2, int(rows))
        with self._lock:
            if self._stopped:
                return
            if (columns, rows) == self._size and self._ready:
                return
            self._size = (columns, rows)
            if self._resize_queued:
                return
            self._resize_queued = True
        self._tasks.put(("resize", None))

    def prioritize_setup(self) -> None:
        """Move this pane to the head of the shared setup queue.

        Called when the tab becomes visible: a terminal the user is looking
        at must never wait behind dozens of background tabs replaying their
        history.
        """

        with self._lock:
            if self._stopped or self._ready:
                return
            self._setup_priority = True
        self._setup_gate.promote(self._setup_token)

    # ------------------------------------------------------- event plumbing

    def _spawn_transport(self) -> subprocess.Popen[bytes]:
        return spawn_control_transport(
            control_attach_argv(self._attach_argv),
            self._environment,
            self._cwd,
        )

    def _handle_state_change(self, state: ClientState) -> None:
        generation: int | None = None
        with self._lock:
            retry_timer = self._take_setup_retry_locked()
            if state is ClientState.CONNECTED:
                self._generation += 1
                self._ready = False
                self._pane_id = None
                self._pane_root_pid = None
                self._pending_output.clear()
                self._pending_output_bytes = 0
                self._pending_output_overflowed = False
                self._setup_attempts = 0
                generation = self._generation
            else:
                self._ready = False
        if retry_timer is not None:
            retry_timer.cancel()
        callback = self._on_state_change
        if callback is not None:
            try:
                # The UI uses CONNECTED as an ordered reset marker. Publish it
                # before the setup worker can emit replay bytes; otherwise two
                # independent GTK idles can race and a late reset can erase the
                # first complete screen of an otherwise healthy attachment.
                callback(state)
            except Exception:
                pass
        if generation is not None:
            self._tasks.put(("setup", generation))

    def _handle_event(self, event: ControlEvent) -> None:
        if event.kind == "output":
            with self._lock:
                if self._pane_id is not None and event.pane_id != self._pane_id:
                    return
                payload = (event.payload or "").encode("latin-1", "replace")
                if not payload:
                    return
                if not self._ready:
                    if self._pending_output_overflowed:
                        return
                    projected = self._pending_output_bytes + len(payload)
                    if (
                        projected > _MAX_PENDING_OUTPUT_BYTES
                        or len(self._pending_output) >= _MAX_PENDING_OUTPUT_CHUNKS
                    ):
                        # Do not retain a suffix whose prefix was discarded.
                        # The setup transaction will retry from a fresh tmux
                        # capture, which is the authoritative recovery source.
                        self._pending_output.clear()
                        self._pending_output_bytes = 0
                        self._pending_output_overflowed = True
                        return
                    self._pending_output.append(payload)
                    self._pending_output_bytes = projected
                    return
            self._emit_output(payload)
            return
        if event.kind == "pane-exited":
            self._report_pane_dead()
            return
        if event.kind == "subscription-changed":
            if event.args and event.args[0] == _DEAD_SUBSCRIPTION:
                _, _, value = event.raw.partition(" : ")
                if value.strip() == "1":
                    self._report_pane_dead()
            return

    def _report_pane_dead(self) -> None:
        with self._lock:
            if self._pane_dead_reported or self._stopped:
                return
            self._pane_dead_reported = True
        callback = self._on_pane_dead
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _emit_output(self, payload: bytes) -> None:
        try:
            self._on_output(payload)
        except Exception:
            pass

    # -------------------------------------------------------------- worker

    def _worker_main(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            with self._lock:
                if self._stopped:
                    return
            kind, argument = task
            try:
                if kind == "setup":
                    self._run_setup(int(argument))  # type: ignore[arg-type]
                elif kind == "resize":
                    self._run_resize()
                elif kind == "input":
                    self._run_input(argument)  # type: ignore[arg-type]
            except Exception:
                # The worker is the only consumer of this queue; it must
                # survive any single failing command to keep input flowing.
                pass

    def _command(self, command: str):
        return self._client.send_command(command, timeout=self._command_timeout)

    def _release_unsent_input(self, data: bytes) -> None:
        with self._lock:
            self._unsent_input_bytes = max(
                0, self._unsent_input_bytes - len(data)
            )
            self._unsent_input_chunks = max(0, self._unsent_input_chunks - 1)

    def _notify_resize_applied(
        self,
        *,
        pane_root_pid: int | None,
        columns: int,
        rows: int,
        wait: bool = False,
    ) -> bool:
        callback = self._on_resize_applied
        if callback is None or pane_root_pid is None:
            return True
        dispatcher = self._resize_signal_dispatcher
        if not wait:
            # Ordinary live resize remains authoritative even when optional
            # containment notification is temporarily at capacity.
            return dispatcher.submit_latest(
                callback,
                pane_root_pid=int(pane_root_pid),
                columns=int(columns),
                rows=int(rows),
            )
        ticket = dispatcher.submit_barrier(
            callback,
            pane_root_pid=int(pane_root_pid),
            columns=int(columns),
            rows=int(rows),
        )
        # Setup requires the attempt to finish before capture, not a successful
        # domain result. Optional callback exceptions are recorded and retried
        # later but must never strand a healthy shell or non-Codex pane.
        if ticket is None:
            return False
        completed = ticket.wait(self._resize_signal_timeout)
        if not completed:
            # A timed-out setup must not leave a stale queued grid to execute
            # after a newer retry. Active callbacks are already bounded and
            # cannot be preempted safely, but pending work observes cancellation.
            ticket.cancel("resize notification timed out")
        return completed

    @staticmethod
    def _setup_nudge_grid(columns: int, rows: int) -> tuple[int, int]:
        if columns > 2:
            return columns - 1, rows
        if rows > 2:
            return columns, rows - 1
        return columns, rows

    def _run_resize(self) -> None:
        with self._lock:
            self._resize_queued = False
            columns, rows = self._size
            pane_root_pid = self._pane_root_pid
        result = self._command(f"refresh-client -C {columns}x{rows}")
        if result.ok:
            self._notify_resize_applied(
                pane_root_pid=pane_root_pid,
                columns=columns,
                rows=rows,
            )

    def _run_input(self, data: bytes) -> None:
        with self._lock:
            pane_id = self._pane_id
            ready = self._ready
            if pane_id is None or not ready:
                if not self._stopped:
                    self._pending_input.append(data)
                    return
        try:
            if pane_id is not None and ready:
                self._send_input_bytes(pane_id, data)
        finally:
            self._release_unsent_input(data)

    def _send_input_bytes(self, pane_id: str, data: bytes) -> bool:
        for start in range(0, len(data), _INPUT_CHUNK_BYTES):
            chunk = data[start : start + _INPUT_CHUNK_BYTES]
            tokens = " ".join(f"{byte:02x}" for byte in chunk)
            result = self._command(f"send-keys -t {pane_id} -H {tokens}")
            if not result.ok:
                # A dead pane rejects input; report once instead of spamming.
                self._report_pane_dead()
                return False
        return True

    def _run_setup(self, generation: int) -> None:
        def abandoned() -> bool:
            with self._lock:
                return generation != self._generation or self._stopped

        with self._lock:
            priority = self._setup_priority
        acquired = self._setup_gate.acquire(
            self._setup_token, priority=priority, should_abort=abandoned
        )
        if not acquired:
            return
        try:
            finished = self._try_setup(generation)
        except Exception:
            # ``_ready`` is the setup transaction's commit point. Before it,
            # the transport is already CONNECTED and no later state edge will
            # restart a failed setup, so retry is required to escape a blank
            # tab. After it, retrying would replay an already-visible pane.
            with self._lock:
                finished = (
                    generation != self._generation
                    or self._stopped
                    or self._ready
                )
        finally:
            self._setup_gate.release()
        if not finished:
            self._schedule_setup_retry(generation)

    def _try_setup(self, generation: int) -> bool:
        with self._lock:
            if generation != self._generation or self._stopped:
                return True
            columns, rows = self._size

        target = self._target
        scope = f" -t {target}" if target else ""

        def read_pane_state() -> dict[str, str] | None:
            reply = self._command(f"list-panes{scope} -F '{_PANE_STATE_FORMAT}'")
            if not reply.ok or not reply.lines:
                return None
            fields = reply.lines[0].split()
            if len(fields) != len(_STATE_FIELDS):
                return None
            state = dict(zip(_STATE_FIELDS, fields))
            try:
                pane_pid = int(state["pane_pid"])
            except (KeyError, ValueError):
                return None
            if pane_pid <= 1:
                return None
            return state

        repair_this_setup = self._on_resize_applied is not None and (
            self._repair_on_setup or self._replayed_history
        )
        if not repair_this_setup:
            # Preserve the historical one-resize/one-state-query transaction for
            # generic controllers and for the first attach of an exact-grid new
            # payload. Subsequent reconnects repair before replay.
            with self._lock:
                if generation != self._generation or self._stopped:
                    return True
                self._pending_output.clear()
                self._pending_output_bytes = 0
                self._pending_output_overflowed = False
            final_resize = self._command(f"refresh-client -C {columns}x{rows}")
            if not final_resize.ok:
                return False
            pane_state = read_pane_state()
            if pane_state is None:
                return False
            pane_id = pane_state["pane_id"]
            pane_root_pid = int(pane_state["pane_pid"])
        else:
            initial_state = read_pane_state()
            if initial_state is None:
                return False
            pane_id = initial_state["pane_id"]
            pane_root_pid = int(initial_state["pane_pid"])
            with self._lock:
                if generation != self._generation or self._stopped:
                    return True
                self._pane_id = pane_id
                self._pane_root_pid = pane_root_pid

            # A control-mode client has no PTY of its own. Repair a stale
            # contained full-screen layout before replay is committed: nudge by
            # one cell, propagate SIGWINCH across the bwrap session boundary,
            # restore the exact VTE grid, and propagate once more.
            nudge_columns, nudge_rows = self._setup_nudge_grid(columns, rows)
            if (nudge_columns, nudge_rows) != (columns, rows):
                nudge = self._command(
                    f"refresh-client -C {nudge_columns}x{nudge_rows}"
                )
                if not nudge.ok:
                    return False
                if not self._notify_resize_applied(
                    pane_root_pid=pane_root_pid,
                    columns=nudge_columns,
                    rows=nudge_rows,
                    wait=True,
                ):
                    return False

            final_resize = self._command(f"refresh-client -C {columns}x{rows}")
            if not final_resize.ok:
                return False
            if not self._notify_resize_applied(
                pane_root_pid=pane_root_pid,
                columns=columns,
                rows=rows,
                wait=True,
            ):
                return False

            # Redraw output generated by the nudge/final transition is already
            # represented by the final authoritative tmux capture. Establish the
            # incremental-output boundary only now.
            with self._lock:
                if generation != self._generation or self._stopped:
                    return True
                self._pending_output.clear()
                self._pending_output_bytes = 0
                self._pending_output_overflowed = False

            pane_state = read_pane_state()
            if pane_state is None:
                return False
            if (
                pane_state["pane_id"] != pane_id
                or int(pane_state["pane_pid"]) != pane_root_pid
            ):
                # A respawn raced the repair. Retry against one coherent
                # replacement identity instead of committing mixed replay.
                return False

        with self._lock:
            if generation != self._generation or self._stopped:
                return True
            self._pane_id = pane_id
            self._pane_root_pid = pane_root_pid

        # Session-scoped subscription (empty "what" field). Pane-scoped
        # subscriptions never emit reliably on tmux 3.7; each managed session
        # hosts one pane, so the active-pane context is exact.
        self._command(
            f"refresh-client -B '{_DEAD_SUBSCRIPTION}::" "#{pane_dead}'"
        )

        replay = self._build_replay(pane_state, rows)
        if replay is None:
            return False

        with self._lock:
            if generation != self._generation or self._stopped:
                return True
            if self._pending_output_overflowed:
                self._pending_output.clear()
                self._pending_output_bytes = 0
                self._pending_output_overflowed = False
                return False
            buffered = b"".join(self._pending_output)
            self._pending_output.clear()
            self._pending_output_bytes = 0
            pending_input = self._pending_input
            self._pending_input = []
            self._replayed_history = True
            self._ready = True
        if replay:
            self._emit_output(replay)
        if buffered:
            self._emit_output(buffered)
        for index, data in enumerate(pending_input):
            try:
                sent = self._send_input_bytes(pane_id, data)
            finally:
                self._release_unsent_input(data)
            if not sent:
                for remaining in pending_input[index + 1 :]:
                    self._release_unsent_input(remaining)
                break
        if pane_state.get("pane_dead") == "1":
            self._report_pane_dead()
        return True

    def _fire_setup_retry(self, generation: int) -> None:
        with self._lock:
            if self._setup_retry_generation != generation:
                return
            self._setup_retry_timer = None
            self._setup_retry_generation = None
            if generation != self._generation or self._stopped:
                return
        self._tasks.put(("setup", generation))

    def _schedule_setup_retry(self, generation: int) -> None:
        """Own at most one bounded retry timer for the current generation."""

        with self._lock:
            if generation != self._generation or self._stopped:
                return
            if self._setup_retry_timer is not None:
                return
            self._setup_attempts += 1
            delay = min(8.0, 0.5 * (2 ** min(self._setup_attempts - 1, 4)))
            timer = threading.Timer(delay, self._fire_setup_retry, args=(generation,))
            timer.daemon = True
            self._setup_retry_timer = timer
            self._setup_retry_generation = generation
        try:
            timer.start()
        except Exception:
            with self._lock:
                if self._setup_retry_timer is timer:
                    self._setup_retry_timer = None
                    self._setup_retry_generation = None
            # A timer-allocation failure is rare but not permission to strand
            # a connected tab. Requeue once through the existing worker path.
            self._tasks.put(("setup", generation))

    # -------------------------------------------------------------- replay

    def _capture(self, pane_id: str, arguments: str) -> list[bytes] | None:
        reply = self._command(f"capture-pane -epq -t {pane_id} {arguments}")
        if not reply.ok:
            return None
        # CommandResult owns the Latin-1 carrier reversal. Keeping that
        # conversion at the protocol boundary prevents replay consumers from
        # guessing an encoding and repeating the historical UTF-8 double pass.
        return list(reply.byte_lines)

    def _build_replay(self, pane_state: dict[str, str], rows: int) -> bytes | None:
        """Reconstruct scrollback, screen, cursor, and modes for a fresh VTE.

        First attach seeds VTE's normal-screen scrollback from tmux history;
        reconnects repaint only the visible screen so history is not
        duplicated.  All content is emitted through the same byte channel the
        live stream uses, so ordering with %output is preserved by the ready
        gate in ``_run_setup``.  Returns ``None`` when a capture command
        fails so the caller retries the whole setup.
        """

        pane_id = pane_state["pane_id"]
        alternate = pane_state.get("alternate_on") == "1"
        parts: list[bytes] = []

        first_attach = not self._replayed_history

        try:
            history_size = max(0, int(pane_state.get("history_size", "0")))
        except ValueError:
            history_size = 0
        if first_attach and self._history_lines and history_size:
            # With an empty history tmux clamps "-E -1" onto the visible
            # screen, which would repeat the first visible row below.
            history = self._capture(
                pane_id,
                f"-J -S -{min(self._history_lines, history_size)} -E -1",
            )
            if history is None:
                return None
            text = b"\r\n".join(line.rstrip() for line in history).rstrip()
            if text:
                parts.append(text + b"\r\n")
        elif not first_attach:
            # Reconnect: repaint over whatever the widget last showed.
            parts.append(b"\x1b[?1049l" if not alternate else b"")

        if alternate:
            parts.append(b"\x1b[?1049h")
        parts.append(b"\x1b[2J\x1b[H" if (alternate or not first_attach) else b"")

        visible = self._capture(pane_id, "-N")
        if visible is None:
            return None
        while len(visible) > rows:
            visible.pop()
        if visible:
            parts.append(b"\r\n".join(visible))

        # Cursor: after feeding, the caret rests at the end of the last fed
        # row.  Walk it to tmux's reported position.
        try:
            cursor_x = max(0, int(pane_state.get("cursor_x", "0")))
            cursor_y = max(0, int(pane_state.get("cursor_y", "0")))
        except ValueError:
            cursor_x, cursor_y = 0, 0
        fed_rows = max(1, len(visible))
        up_moves = max(0, min(fed_rows - 1, (fed_rows - 1) - cursor_y))
        parts.append(b"\r")
        if up_moves:
            parts.append(f"\x1b[{up_moves}A".encode("ascii"))
        if cursor_x:
            parts.append(f"\x1b[{cursor_x}C".encode("ascii"))

        for field, active_value, set_seq, reset_seq in _MODE_SEQUENCES:
            value = pane_state.get(field)
            if value == active_value and set_seq:
                parts.append(set_seq)
            elif value != active_value and reset_seq:
                parts.append(reset_seq)

        return b"".join(part for part in parts if part)
