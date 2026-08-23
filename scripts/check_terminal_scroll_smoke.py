#!/usr/bin/env python3
"""Verify application-aware history, VTE selection, and TUI wheel routing.

The persistent scrollbar owns Termdeck's tmux/TUI history model. VTE's native
adjustment remains separate and is exposed only while a real text selection owns
the viewport. Wheel navigation must be incremental, must never leak shell arrow
keys into ordinary shells or composers. Alternate-screen applications that
negotiate mouse reporting retain that protocol in both directions, including the
persistent scrollbar. Replayed focus/bracketed-paste modes must preserve composer
input, Ctrl+L redraw, and a complete bottom-of-screen frame; no-mouse TUIs receive
neither synthetic arrows nor an unrelated tmux history surface.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _drain(Gtk, *, until=None, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.01)
    raise RuntimeError("GTK terminal state did not settle")


def _wheel(terminal, button: int, *, clicks: int = 1) -> None:
    from termdeck.gtk_app import Gdk, Gtk

    direction = Gdk.ScrollDirection.UP if button == 4 else Gdk.ScrollDirection.DOWN
    window = terminal.get_window()
    if window is None:
        raise RuntimeError("terminal has no mapped GDK window")
    _ok, root_x, root_y = window.get_origin()
    for _index in range(max(1, int(clicks))):
        event = Gdk.Event.new(Gdk.EventType.SCROLL)
        event.scroll.window = window
        event.scroll.send_event = True
        event.scroll.time = Gdk.CURRENT_TIME
        event.scroll.x = 80.0
        event.scroll.y = 80.0
        event.scroll.x_root = float(root_x + 80)
        event.scroll.y_root = float(root_y + 80)
        event.scroll.state = Gdk.ModifierType(0)
        event.scroll.direction = direction
        terminal.event(event)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)


def _wheel_up(terminal, *, clicks: int = 1) -> None:
    _wheel(terminal, 4, clicks=clicks)


def _wheel_down(terminal, *, clicks: int = 1) -> None:
    _wheel(terminal, 5, clicks=clicks)


def _events(path: Path) -> list[str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _adjustment_state(adjustment) -> dict[str, float]:
    lower = float(adjustment.get_lower())
    upper = float(adjustment.get_upper())
    page_size = float(adjustment.get_page_size())
    value = float(adjustment.get_value())
    return {
        "lower": lower,
        "upper": upper,
        "page_size": page_size,
        "value": value,
        "bottom": max(lower, upper - page_size),
    }


def _pane_modes(app, record) -> tuple[int, int]:
    result = app.backend._run(
        [
            "display-message",
            "-p",
            "-t",
            app.backend.pane_target(record),
            "#{alternate_on} #{pane_in_mode}",
        ],
        check=False,
    )
    fields = result.stdout.strip().split()
    return (int(fields[0]), int(fields[1])) if len(fields) == 2 else (0, 0)


def _pane_input_modes(app, record) -> dict[str, int]:
    result = app.backend._run(
        [
            "display-message",
            "-p",
            "-t",
            app.backend.pane_target(record),
            (
                "#{alternate_on} #{pane_in_mode} #{mouse_standard_flag} "
                "#{mouse_all_flag} #{mouse_sgr_flag} #{bracket_paste_flag}"
            ),
        ],
        check=False,
    )
    fields = result.stdout.strip().split()
    names = (
        "alternate",
        "tmux_copy_mode",
        "mouse_standard",
        "mouse_all",
        "mouse_sgr",
        "bracketed_paste",
    )
    if len(fields) != len(names):
        return {name: 0 for name in names}
    return {name: int(value) for name, value in zip(names, fields)}


def _pane_capture(app, record) -> str:
    result = app.backend._run(
        [
            "capture-pane",
            "-p",
            "-t",
            app.backend.pane_target(record),
            "-S",
            "-120",
        ],
        check=False,
    )
    return result.stdout


def _complete_composer(text: str) -> bool:
    return all(
        marker in text
        for marker in (
            "T1000_NATIVE_FRAME_BEGIN",
            "T1000_COMPOSER_TOP",
            "T1000_COMPOSER_BOTTOM",
            "T1000_NATIVE_FRAME_END",
        )
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-scroll-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        shell_ready = root / "shell-ready"
        shell_input = root / "shell-input.bin"
        shell_worker = root / "shell_worker.py"
        shell_worker.write_text(
            """import os, pathlib, sys, termios, time
ready, captured = map(pathlib.Path, sys.argv[1:])
for index in range(300):
    print(f"scrollback line {index}")
sys.stdout.flush()
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 0
raw[6][termios.VTIME] = 1
termios.tcsetattr(fd, termios.TCSANOW, raw)
ready.touch()
data = b""
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    data += os.read(fd, 1024)
    captured.write_bytes(data)
termios.tcsetattr(fd, termios.TCSANOW, old)
""",
            encoding="utf-8",
        )

        fallback_ready = root / "fallback-ready"
        fallback_events = root / "fallback-events.json"
        fallback_simulator = root / "fallback_simulator.py"
        fallback_simulator.write_text(
            """import json, os, pathlib, sys, termios, time
ready, event_path = map(pathlib.Path, sys.argv[1:])
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 0
raw[6][termios.VTIME] = 1
termios.tcsetattr(fd, termios.TCSANOW, raw)
sys.stdout.write("\\x1b[?1049hAlternate-screen fallback TUI\\r\\n")
sys.stdout.flush()
ready.touch()
events = []
pending = b""
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    pending += os.read(fd, 1024)
    changed = False
    while pending:
        if pending.startswith(b"\\x1b[A") or pending.startswith(b"\\x1bOA"):
            width = 3
            events.append("up")
        elif pending.startswith(b"\\x1b[B") or pending.startswith(b"\\x1bOB"):
            width = 3
            events.append("down")
        elif pending[0] == ord("x"):
            width = 1
            events.append("x")
        elif pending[0] == 0x1B and len(pending) < 3:
            break
        else:
            width = 1
        pending = pending[width:]
        changed = True
    if changed:
        event_path.write_text(json.dumps(events), encoding="utf-8")
termios.tcsetattr(fd, termios.TCSANOW, old)
sys.stdout.write("\\x1b[?1049l")
sys.stdout.flush()
""",
            encoding="utf-8",
        )

        native_ready = root / "native-ready"
        native_input = root / "native-input.bin"
        native_simulator = root / "native_simulator.py"
        native_simulator.write_text(
            """import os, pathlib, signal, sys, termios, time
ready, captured = map(pathlib.Path, sys.argv[1:])
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 0
raw[6][termios.VTIME] = 1
termios.tcsetattr(fd, termios.TCSANOW, raw)
redraw = True

def request_redraw(*_args):
    global redraw
    redraw = True

def render():
    global redraw
    size = os.get_terminal_size(fd)
    columns = max(24, int(size.columns))
    rows = max(8, int(size.lines))
    def paint(row, text):
        clipped = text[:columns].ljust(columns)
        sys.stdout.write(f"\\x1b[{row};1H{clipped}")
    sys.stdout.write("\\x1b[2J\\x1b[H")
    paint(1, f"T1000_NATIVE_FRAME_BEGIN {columns}x{rows}")
    paint(max(2, rows - 4), "+-- T1000_COMPOSER_TOP --+")
    paint(max(3, rows - 3), "| draft remains editable; Enter is explicit |")
    paint(max(4, rows - 2), "+-- T1000_COMPOSER_BOTTOM --+")
    paint(rows, "T1000_NATIVE_FRAME_END")
    sys.stdout.write(f"\\x1b[{max(3, rows - 3)};3H")
    sys.stdout.flush()
    redraw = False

signal.signal(signal.SIGWINCH, request_redraw)
sys.stdout.write(
    "\\x1b[?1049h\\x1b[?1000h\\x1b[?1003h"
    "\\x1b[?1004h\\x1b[?1006h\\x1b[?2004h"
)
sys.stdout.flush()
captured.write_bytes(b"")
ready.touch()
data = b""
deadline = time.monotonic() + 45.0
while time.monotonic() < deadline:
    if redraw:
        render()
    chunk = os.read(fd, 4096)
    if not chunk:
        continue
    data += chunk
    captured.write_bytes(data)
    if b"\\x0c" in chunk:
        request_redraw()
termios.tcsetattr(fd, termios.TCSANOW, old)
sys.stdout.write(
    "\\x1b[?2004l\\x1b[?1006l\\x1b[?1004l"
    "\\x1b[?1003l\\x1b[?1000l\\x1b[?1049l"
)
sys.stdout.flush()
""",
            encoding="utf-8",
        )

        fallback_executable = root / "codex"
        fallback_executable.symlink_to(sys.executable)
        native_executable = root / "2.1.226"
        native_executable.symlink_to(sys.executable)
        shell_command = shlex.join(
            [sys.executable, str(shell_worker), str(shell_ready), str(shell_input)]
        )
        fallback_command = shlex.join(
            [
                str(fallback_executable),
                str(fallback_simulator),
                str(fallback_ready),
                str(fallback_events),
            ]
        )
        native_command = shlex.join(
            [str(native_executable), str(native_simulator), str(native_ready), str(native_input)]
        )
        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Scroll QA",
                    "windows": [
                        {
                            "title": "Scroll QA",
                            "tabs": [
                                {
                                    "name": "shell-history",
                                    "command": shell_command,
                                    "cwd": str(root),
                                },
                                {
                                    "name": "alternate-fallback",
                                    "command": fallback_command,
                                    "cwd": str(root),
                                },
                                {
                                    "name": "native-mouse",
                                    "command": native_command,
                                    "cwd": str(root),
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from termdeck.gtk_app import Gtk, TermDeckApplication, Vte
        from termdeck.history_control import HistoryOwner, ScrollDisposition

        app = TermDeckApplication(db_path=root / "tabs.sqlite3", workspace_path=workspace)
        app.set_application_id(f"dev.termdeck.ScrollSmoke.p{os.getpid()}")
        try:
            app.register(None)
            app.activate()
            _drain(
                Gtk,
                until=lambda: bool(app.windows and len(app.windows[0].tabs) == 3),
            )
            window = app.windows[0]
            records = {record.name: record for record in app.store.list()}
            shell_record = records["shell-history"]
            fallback_record = records["alternate-fallback"]
            native_record = records["native-mouse"]

            # The persistent bar owns application-aware tmux history. VTE's
            # adjustment remains available for selection review only.
            _drain(Gtk, until=shell_ready.exists)
            window._focus_session(shell_record.id)
            shell_attach = window.tabs[shell_record.id].widget
            shell_terminal = shell_attach.terminal
            shell_history = shell_attach.history_adjustment
            shell_native = shell_attach.native_adjustment
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == shell_record.id
                    and shell_terminal.get_mapped()
                    and getattr(shell_attach.controller, "_ready", False)
                ),
            )
            if shell_attach.scrollbar.get_adjustment() is not shell_history:
                raise AssertionError(
                    "shell scrollbar is not bound to application-aware history"
                )
            if shell_history is shell_native:
                raise AssertionError("application history was conflated with VTE selection")
            shell_attach.scroll_to_bottom()
            _drain(
                Gtk,
                until=lambda: _adjustment_state(shell_history)["value"]
                >= _adjustment_state(shell_history)["bottom"] - 0.5,
            )
            before = _adjustment_state(shell_history)
            shell_bytes_before_down = shell_input.read_bytes() if shell_input.exists() else b""

            _wheel_up(shell_terminal, clicks=3)
            _drain(
                Gtk,
                until=lambda: _adjustment_state(shell_history)["value"]
                < before["value"] - 2,
            )
            after_up = _adjustment_state(shell_history)
            if _pane_modes(app, shell_record)[1] != 1:
                raise AssertionError(
                    "application-aware shell history did not enter its tmux-owned review surface"
                )

            # One down step from history is incremental; it must not throw the
            # viewport back to the composer/live edge.
            _wheel_down(shell_terminal)
            _drain(
                Gtk,
                until=lambda: _adjustment_state(shell_history)["value"]
                > after_up["value"] + 0.5,
            )
            after_down = _adjustment_state(shell_history)
            down_delta = after_down["value"] - after_up["value"]
            if down_delta > 10.0 or after_down["value"] >= after_down["bottom"] - 0.5:
                raise AssertionError(
                    "one application-history wheel-down jumped to the live edge: "
                    f"before={after_up}, after={after_down}"
                )
            shell_bytes_after_down = shell_input.read_bytes() if shell_input.exists() else b""
            wheel_bytes = shell_bytes_after_down[len(shell_bytes_before_down) :]
            if b"\x1b[A" in wheel_bytes or b"\x1b[B" in wheel_bytes:
                raise AssertionError("normal-screen wheel leaked arrow keys into the shell")

            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(
                Gtk,
                until=lambda: shell_input.exists() and b"x" in shell_input.read_bytes(),
            )
            _drain(
                Gtk,
                until=lambda: _adjustment_state(shell_history)["value"]
                >= _adjustment_state(shell_history)["bottom"] - 0.5,
            )
            shell_final = _adjustment_state(shell_history)
            if _pane_modes(app, shell_record)[1] != 0:
                raise AssertionError("live terminal input did not leave tmux history review")

            # A no-mouse alternate-screen app must not receive synthetic
            # Up/Down keys from a physical wheel. Those keys can mutate a TUI
            # composer or selector. Termdeck also must not open tmux copy mode
            # over the application's alternate screen.
            _drain(Gtk, until=fallback_ready.exists)
            window._focus_session(fallback_record.id)
            fallback_attach = window.tabs[fallback_record.id].widget
            fallback_terminal = fallback_attach.terminal
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == fallback_record.id
                    and fallback_terminal.get_mapped()
                    and getattr(fallback_attach.controller, "_ready", False)
                    and _pane_modes(app, fallback_record)[0] == 1
                ),
            )
            fallback_before_wheel = list(_events(fallback_events))
            _wheel_up(fallback_terminal)
            _wheel_down(fallback_terminal)
            _drain(Gtk)
            fallback_after_wheel = list(_events(fallback_events))
            if fallback_after_wheel != fallback_before_wheel:
                raise AssertionError(
                    "no-mouse alternate-screen wheel synthesized child input: "
                    f"before={fallback_before_wheel}, after={fallback_after_wheel}"
                )
            if _pane_modes(app, fallback_record)[1] != 0:
                raise AssertionError("alternate fallback wheel entered tmux copy mode")
            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: "x" in _events(fallback_events))
            fallback_final_events = _events(fallback_events)

            # A mouse-aware alternate-screen app receives its negotiated SGR
            # wheel protocol in both directions, including persistent-scrollbar
            # drags. Replay must also restore bracketed-paste mode so Ctrl+L
            # redraw and multiline composer text remain application-owned.
            _drain(Gtk, until=native_ready.exists)
            window._focus_session(native_record.id)
            native_attach = window.tabs[native_record.id].widget
            native_terminal = native_attach.terminal
            native_history = native_attach.history_adjustment
            expected_native_modes = {
                "alternate": 1,
                "tmux_copy_mode": 0,
                # DECSET 1003 supersedes DECSET 1000 in tmux's pane state:
                # all-motion reporting is active and the standard-only flag is clear.
                "mouse_standard": 0,
                "mouse_all": 1,
                "mouse_sgr": 1,
                "bracketed_paste": 1,
            }

            def _native_ready_observation() -> tuple[bool, dict[str, object]]:
                selected = window.current_session_id() == native_record.id
                mapped = bool(native_terminal.get_mapped())
                stream_ready = bool(getattr(native_attach.controller, "_ready", False))
                modes = _pane_input_modes(app, native_record) if stream_ready else {}
                tmux_frame = _pane_capture(app, native_record) if stream_ready else ""
                vte_frame = (
                    native_terminal.get_text_format(Vte.Format.TEXT)
                    if stream_ready
                    else ""
                )
                observation = {
                    "selected": selected,
                    "mapped": mapped,
                    "stream_ready": stream_ready,
                    "modes": modes,
                    "expected_modes": expected_native_modes,
                    "tmux_complete": _complete_composer(tmux_frame),
                    "vte_complete": _complete_composer(vte_frame),
                    "tmux_tail": tmux_frame[-2000:],
                    "vte_tail": vte_frame[-2000:],
                }
                return (
                    selected
                    and mapped
                    and stream_ready
                    and modes == expected_native_modes
                    and bool(observation["tmux_complete"])
                    and bool(observation["vte_complete"]),
                    observation,
                )

            try:
                _drain(Gtk, until=lambda: _native_ready_observation()[0])
            except RuntimeError as exc:
                _ready, observation = _native_ready_observation()
                raise AssertionError(
                    "native composer did not reach a complete replay state: "
                    + json.dumps(observation, sort_keys=True)
                ) from exc
            native_initial_modes = _pane_input_modes(app, native_record)
            native_initial_tmux_frame = _pane_capture(app, native_record)
            native_initial_vte_frame = native_terminal.get_text_format(Vte.Format.TEXT)
            native_attach.scroll_to_bottom()
            _drain(
                Gtk,
                until=lambda: _adjustment_state(native_history)["value"]
                >= _adjustment_state(native_history)["bottom"] - 0.5,
            )
            native_before = _adjustment_state(native_history)

            # Physical wheel up/down are delivered by VTE under the replayed
            # mouse modes; neither direction may enter tmux copy mode.
            native_bytes = native_input.read_bytes()
            _wheel_up(native_terminal)
            _drain(
                Gtk,
                until=lambda: b"\x1b[<64;"
                in native_input.read_bytes()[len(native_bytes) :],
            )
            physical_up_slice = native_input.read_bytes()[len(native_bytes) :]
            native_bytes = native_input.read_bytes()
            _wheel_down(native_terminal)
            _drain(
                Gtk,
                until=lambda: b"\x1b[<65;"
                in native_input.read_bytes()[len(native_bytes) :],
            )
            physical_down_slice = native_input.read_bytes()[len(native_bytes) :]

            # The persistent bar uses the same negotiated child protocol rather
            # than sticking at the endpoint or reverting on PASSTHROUGH.
            owner_probe = native_attach._scroll_handler(-9)
            if not (
                owner_probe.disposition is ScrollDisposition.PASSTHROUGH
                and owner_probe.owner is HistoryOwner.CHILD_TUI
            ):
                raise AssertionError(
                    "mouse-aware alternate-screen owner was misclassified: "
                    + repr(owner_probe)
                )
            native_bytes = native_input.read_bytes()
            native_history.set_value(native_before["bottom"] - 9.0)

            def _scrollbar_up_observation() -> tuple[bool, dict[str, object]]:
                payload = native_input.read_bytes()[len(native_bytes) :]
                adjustment = _adjustment_state(native_history)
                observation = {
                    "input_hex": payload.hex(),
                    "adjustment": adjustment,
                    "child_depth": int(native_attach._child_tui_history_depth),
                    "last_applied_sequence": int(
                        native_attach._history_last_applied_sequence
                    ),
                    "worker_active": bool(native_attach._history_worker_active),
                    "pending_delta": int(native_attach._pending_history_delta),
                    "pending_apply": native_attach._pending_history_apply is not None,
                    "apply_scheduled": bool(native_attach._history_apply_scheduled),
                    "last_value": float(native_attach._last_history_scrollbar_value),
                    "last_requested": float(
                        native_attach._history_drag_last_requested
                    ),
                    "stream_scheduling_error": str(
                        native_attach._last_stream_scheduling_error
                    ),
                }
                return (
                    b"\x1b[<64;" in payload
                    and adjustment["value"] <= native_before["bottom"] - 8.5,
                    observation,
                )

            try:
                _drain(Gtk, until=lambda: _scrollbar_up_observation()[0])
            except RuntimeError as exc:
                _ready, observation = _scrollbar_up_observation()
                raise AssertionError(
                    "child-owned scrollbar up did not settle: "
                    + json.dumps(observation, sort_keys=True)
                ) from exc
            native_scrollbar_up = _adjustment_state(native_history)
            scrollbar_up_slice = native_input.read_bytes()[len(native_bytes) :]
            native_bytes = native_input.read_bytes()
            native_history.set_value(native_before["bottom"])

            def _scrollbar_down_observation() -> tuple[bool, dict[str, object]]:
                payload = native_input.read_bytes()[len(native_bytes) :]
                adjustment = _adjustment_state(native_history)
                observation = {
                    "input_hex": payload.hex(),
                    "adjustment": adjustment,
                    "child_depth": int(native_attach._child_tui_history_depth),
                    "last_applied_sequence": int(
                        native_attach._history_last_applied_sequence
                    ),
                    "worker_active": bool(native_attach._history_worker_active),
                    "pending_delta": int(native_attach._pending_history_delta),
                    "pending_apply": native_attach._pending_history_apply is not None,
                    "apply_scheduled": bool(native_attach._history_apply_scheduled),
                    "last_value": float(native_attach._last_history_scrollbar_value),
                    "last_requested": float(
                        native_attach._history_drag_last_requested
                    ),
                    "stream_scheduling_error": str(
                        native_attach._last_stream_scheduling_error
                    ),
                }
                return (
                    b"\x1b[<65;" in payload
                    and adjustment["value"] >= native_before["bottom"] - 0.5,
                    observation,
                )

            try:
                _drain(Gtk, until=lambda: _scrollbar_down_observation()[0])
            except RuntimeError as exc:
                _ready, observation = _scrollbar_down_observation()
                raise AssertionError(
                    "child-owned scrollbar down did not settle: "
                    + json.dumps(observation, sort_keys=True)
                ) from exc
            native_scrollbar_down = _adjustment_state(native_history)
            scrollbar_down_slice = native_input.read_bytes()[len(native_bytes) :]

            # Ctrl+L must reach the application as form-feed and trigger a full
            # redraw; Termdeck must not reserve or swallow it.
            native_terminal.grab_focus()
            _drain(Gtk)
            native_bytes = native_input.read_bytes()
            subprocess.run(["xdotool", "key", "ctrl+l"], check=True, timeout=5)
            _drain(
                Gtk,
                until=lambda: b"\x0c"
                in native_input.read_bytes()[len(native_bytes) :]
                and _complete_composer(_pane_capture(app, native_record))
                and _complete_composer(
                    native_terminal.get_text_format(Vte.Format.TEXT)
                ),
            )
            ctrl_l_slice = native_input.read_bytes()[len(native_bytes) :]
            native_after_ctrl_l_tmux = _pane_capture(app, native_record)
            native_after_ctrl_l_vte = native_terminal.get_text_format(Vte.Format.TEXT)

            # Ordinary composer text must remain unsubmitted. No CR/Enter may be
            # synthesized merely because the user typed into the focused VTE.
            draft = b"T1000_DRAFT_STAYS_EDITABLE"
            native_bytes = native_input.read_bytes()
            subprocess.run(
                ["xdotool", "type", "--delay", "5", draft.decode("ascii")],
                check=True,
                timeout=5,
            )
            _drain(
                Gtk,
                until=lambda: draft
                in native_input.read_bytes()[len(native_bytes) :],
            )
            draft_slice = native_input.read_bytes()[len(native_bytes) :]
            if draft + b"\r" in draft_slice or draft + b"\n" in draft_slice:
                raise AssertionError("ordinary composer text was auto-submitted")

            # Multiline paste must be one DECSET-2004 transaction; its internal
            # newline is protected and no submit follows the closing wrapper.
            native_bytes = native_input.read_bytes()
            paste_prefix = b"\x1b[200~T1000_PASTE_ONE"
            paste_suffix = b"T1000_PASTE_TWO\x1b[201~"
            expected_pastes = (
                paste_prefix + b"\r" + paste_suffix,
                paste_prefix + b"\n" + paste_suffix,
            )
            if not native_attach._feed_text("T1000_PASTE_ONE\nT1000_PASTE_TWO"):
                raise AssertionError("composer paste was rejected")

            def _protected_paste_observed() -> bool:
                observed = native_input.read_bytes()[len(native_bytes) :]
                return any(candidate in observed for candidate in expected_pastes)

            try:
                _drain(Gtk, until=_protected_paste_observed)
            except RuntimeError as exc:
                observed = native_input.read_bytes()[len(native_bytes) :]
                raise AssertionError(
                    "composer paste did not retain its bracketed transaction: "
                    + json.dumps(
                        {
                            "expected_hex": [
                                candidate.hex() for candidate in expected_pastes
                            ],
                            "observed_hex": observed.hex(),
                            "pane_modes": _pane_input_modes(app, native_record),
                            "stream_ready": bool(
                                getattr(native_attach.controller, "_ready", False)
                            ),
                        },
                        sort_keys=True,
                    )
                ) from exc
            paste_slice = native_input.read_bytes()[len(native_bytes) :]
            expected_paste = next(
                candidate for candidate in expected_pastes if candidate in paste_slice
            )
            if b"\x1b[201~\r" in paste_slice:
                raise AssertionError("bracketed composer paste was followed by submit")

            native_final_modes = _pane_input_modes(app, native_record)
            native_alt, native_mode = _pane_modes(app, native_record)
            if native_mode != 0:
                raise AssertionError("child-owned scrolling entered tmux copy mode")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "history_owner": "application-aware-history-proxy",
                        "shell_before": before,
                        "shell_after_up": after_up,
                        "shell_after_single_down": after_down,
                        "shell_single_down_delta": round(down_delta, 3),
                        "shell_final_live_edge": shell_final,
                        "shell_tmux_copy_mode": _pane_modes(app, shell_record)[1],
                        "shell_first_key": "x",
                        "alternate_fallback_events": fallback_final_events,
                        "alternate_fallback_tmux_copy_mode": _pane_modes(
                            app, fallback_record
                        )[1],
                        "native_alternate_screen": native_alt,
                        "native_tmux_copy_mode": native_mode,
                        "native_initial_modes": native_initial_modes,
                        "native_final_modes": native_final_modes,
                        "native_mouse_wheel_up": b"\x1b[<64;" in physical_up_slice,
                        "native_mouse_wheel_down": b"\x1b[<65;" in physical_down_slice,
                        "native_scrollbar_before": native_before,
                        "native_scrollbar_after_up": native_scrollbar_up,
                        "native_scrollbar_after_down": native_scrollbar_down,
                        "native_scrollbar_wheel_up": b"\x1b[<64;" in scrollbar_up_slice,
                        "native_scrollbar_wheel_down": b"\x1b[<65;" in scrollbar_down_slice,
                        "native_ctrl_l_delivered": b"\x0c" in ctrl_l_slice,
                        "native_draft_auto_submitted": bool(
                            draft + b"\r" in draft_slice or draft + b"\n" in draft_slice
                        ),
                        "native_bracketed_paste": expected_paste in paste_slice,
                        "native_complete_tmux_frame_initial": _complete_composer(
                            native_initial_tmux_frame
                        ),
                        "native_complete_vte_frame_initial": _complete_composer(
                            native_initial_vte_frame
                        ),
                        "native_complete_tmux_frame_after_ctrl_l": _complete_composer(
                            native_after_ctrl_l_tmux
                        ),
                        "native_complete_vte_frame_after_ctrl_l": _complete_composer(
                            native_after_ctrl_l_vte
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for record in app.store.list():
                try:
                    app.backend.terminate(record)
                except Exception:
                    pass
            for top in list(Gtk.Window.list_toplevels()):
                try:
                    top.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
