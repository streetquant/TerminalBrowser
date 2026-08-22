#!/usr/bin/env python3
"""Verify application-aware history, VTE selection, and TUI wheel routing.

The persistent scrollbar owns Termdeck's tmux/TUI history model. VTE's native
adjustment remains separate and is exposed only while a real text selection owns
the viewport. Wheel navigation must be incremental, must never leak shell arrow
keys into ordinary shells or composers. Alternate-screen applications that
negotiate mouse reporting retain that protocol; no-mouse TUIs receive neither
synthetic arrows nor an unrelated tmux history surface.
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
            """import os, pathlib, sys, termios, time
ready, captured = map(pathlib.Path, sys.argv[1:])
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 0
raw[6][termios.VTIME] = 1
termios.tcsetattr(fd, termios.TCSANOW, raw)
sys.stdout.write("\\x1b[?1049h\\x1b[?1000h\\x1b[?1006hNative mouse TUI\\r\\n")
sys.stdout.flush()
captured.write_bytes(b"")
ready.touch()
data = b""
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    data += os.read(fd, 1024)
    captured.write_bytes(data)
termios.tcsetattr(fd, termios.TCSANOW, old)
sys.stdout.write("\\x1b[?1006l\\x1b[?1000l\\x1b[?1049l")
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

        from termdeck.gtk_app import Gtk, TermDeckApplication

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
            # wheel protocol, still without tmux copy mode.
            _drain(Gtk, until=native_ready.exists)
            window._focus_session(native_record.id)
            native_attach = window.tabs[native_record.id].widget
            native_terminal = native_attach.terminal
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == native_record.id
                    and native_terminal.get_mapped()
                    and getattr(native_attach.controller, "_ready", False)
                    and _pane_modes(app, native_record) == (1, 0)
                ),
            )
            _wheel_up(native_terminal)
            _drain(
                Gtk,
                until=lambda: native_input.exists()
                and b"\x1b[<64;" in native_input.read_bytes(),
            )
            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: b"x" in native_input.read_bytes())
            native_alt, native_mode = _pane_modes(app, native_record)

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
                        "native_mouse_wheel": b"\x1b[<64;" in native_input.read_bytes(),
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
