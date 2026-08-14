#!/usr/bin/env python3
"""Verify shell history, Codex transcript, and native alternate-screen scrolling."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _drain(Gtk, *, until=None, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.01)
    raise RuntimeError("GTK terminal state did not settle")


def _wheel(terminal, button: int) -> None:
    _ok, x, y = terminal.get_window().get_origin()
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(x + 80), str(y + 80), "click", str(button)],
        check=True,
        timeout=5,
    )


def _wheel_up(terminal) -> None:
    _wheel(terminal, 4)


def _wheel_down(terminal) -> None:
    _wheel(terminal, 5)


def _events(path: Path) -> list[str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
deadline = time.monotonic() + 20.0
while time.monotonic() < deadline:
    data += os.read(fd, 1024)
    captured.write_bytes(data)
termios.tcsetattr(fd, termios.TCSANOW, old)
""",
            encoding="utf-8",
        )

        codex_ready = root / "codex-ready"
        codex_events = root / "codex-events.json"
        codex_simulator = root / "codex_simulator.py"
        codex_simulator.write_text(
            """import json, os, pathlib, sys, termios, time
ready, event_path = map(pathlib.Path, sys.argv[1:])
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 0
raw[6][termios.VTIME] = 1
termios.tcsetattr(fd, termios.TCSANOW, raw)
sys.stdout.write("\\x1b[?1049hCodex composer\\r\\n")
sys.stdout.flush()
ready.touch()
events = []
transcript = False
pending = b""
deadline = time.monotonic() + 20.0
while time.monotonic() < deadline:
    pending += os.read(fd, 1024)
    changed = False
    while pending:
        if pending.startswith(b"\\x1b[A") or pending.startswith(b"\\x1bOA"):
            width = 3
            events.append("transcript-up" if transcript else "composer-up")
        elif pending.startswith(b"\\x1b[B") or pending.startswith(b"\\x1bOB"):
            width = 3
            events.append("transcript-down" if transcript else "composer-down")
        elif pending.startswith(b"\x1b[5~"):
            width = 4
            events.append("transcript-page-up" if transcript else "composer-page-up")
        elif pending.startswith(b"\x1b[6~"):
            width = 4
            events.append("transcript-page-down" if transcript else "composer-page-down")
        elif pending[0] == 0x14:
            width = 1
            transcript = not transcript
            events.append("transcript-open" if transcript else "transcript-close")
        elif pending[0] == ord("x"):
            width = 1
            events.append("transcript-x" if transcript else "composer-x")
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
sys.stdout.write("\\x1b[?1049h\\x1b[?1000h\\x1b[?1006hNative alternate-screen TUI\\r\\n")
sys.stdout.flush()
captured.write_bytes(b"")
ready.touch()
data = b""
deadline = time.monotonic() + 20.0
while time.monotonic() < deadline:
    data += os.read(fd, 1024)
    captured.write_bytes(data)
termios.tcsetattr(fd, termios.TCSANOW, old)
sys.stdout.write("\\x1b[?1006l\\x1b[?1000l\\x1b[?1049l")
sys.stdout.flush()
""",
            encoding="utf-8",
        )
        codex_executable = root / "codex"
        codex_executable.symlink_to(sys.executable)
        native_executable = root / "2.1.226"
        native_executable.symlink_to(sys.executable)

        shell_command = shlex.join(
            [sys.executable, str(shell_worker), str(shell_ready), str(shell_input)]
        )
        codex_command = shlex.join(
            [str(codex_executable), str(codex_simulator), str(codex_ready), str(codex_events)]
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
                                {"name": "shell-history", "command": shell_command, "cwd": str(root)},
                                {"name": "codex-transcript", "command": codex_command, "cwd": str(root)},
                                {"name": "native-alternate", "command": native_command, "cwd": str(root)},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(db_path=root / "tabs.sqlite3", workspace_path=workspace)
        # A developer may already have the real TermDeck application running on
        # the session bus. Use a per-process ID so this smoke activates its own
        # isolated GTK instance instead of forwarding to that live application.
        app.set_application_id(f"dev.termdeck.ScrollSmoke.p{os.getpid()}")
        try:
            app.register(None)
            app.activate()
            try:
                _drain(
                    Gtk,
                    until=lambda: bool(app.windows and len(app.windows[0].tabs) == 3),
                )
            except RuntimeError as exc:
                state = {
                    "window_count": len(app.windows),
                    "tab_counts": [len(window.tabs) for window in app.windows],
                    "records": [
                        {"name": record.name, "status": record.status, "visible": record.visible}
                        for record in app.store.list()
                    ],
                }
                raise RuntimeError(f"workspace did not attach both scroll fixtures: {state}") from exc
            window = app.windows[0]
            records = {record.name: record for record in app.store.list()}
            shell_record = records["shell-history"]
            codex_record = records["codex-transcript"]
            native_record = records["native-alternate"]

            _drain(Gtk, until=shell_ready.exists)
            window._focus_session(shell_record.id)
            shell_attach = window.tabs[shell_record.id].widget
            shell_terminal = shell_attach.terminal
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == shell_record.id
                    and shell_terminal.get_mapped()
                ),
            )
            _wheel_up(shell_terminal)

            def shell_scroll_state() -> tuple[int, int]:
                result = app.backend._run(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        app.backend.pane_target(shell_record),
                        "#{pane_in_mode} #{scroll_position}",
                    ],
                    check=False,
                )
                fields = result.stdout.strip().split()
                return (int(fields[0]), int(fields[1])) if len(fields) == 2 else (0, 0)

            try:
                _drain(Gtk, until=lambda: shell_scroll_state()[1] >= 3)
            except RuntimeError as exc:
                pane_state = app.backend._run(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        app.backend.pane_target(shell_record),
                        "#{alternate_on}|#{pane_in_mode}|#{history_size}|#{pane_current_command}",
                    ],
                    check=False,
                ).stdout.strip()
                raise RuntimeError(
                    "shell wheel did not enter tmux history: "
                    f"pane={pane_state!r}, tracked={app.backend._scroll_views!r}"
                ) from exc
            shell_mode, shell_position = shell_scroll_state()
            shell_cursor_hidden_during_history = shell_attach._history_cursor_hidden
            if not shell_cursor_hidden_during_history:
                raise AssertionError("shell history left a roaming terminal cursor visible")
            if shell_attach.scrollbar.get_adjustment() is not shell_attach.history_adjustment:
                raise AssertionError("normal shell review scrollbar is not application-aware")
            if shell_attach.history_adjustment is shell_attach.native_adjustment:
                raise AssertionError("shell history proxy aliases VTE native scrollback")

            scrollbar_before_drag = float(shell_attach.history_adjustment.get_value())
            scrollbar_target = max(
                float(shell_attach.history_adjustment.get_lower()),
                scrollbar_before_drag - 30.0,
            )
            if scrollbar_target >= scrollbar_before_drag - 0.5:
                raise AssertionError("application history scrollbar has no usable review range")
            shell_attach.history_adjustment.set_value(scrollbar_target)
            _drain(
                Gtk,
                until=lambda: (
                    not shell_attach._history_worker_active
                    and shell_scroll_state()[1] >= shell_position + 25
                ),
            )
            shell_drag_mode, shell_drag_position = shell_scroll_state()
            if shell_drag_mode != 1 or shell_drag_position <= shell_position:
                raise AssertionError(
                    "dragging the application scrollbar did not move tmux history: "
                    f"{shell_position} -> {shell_drag_position}"
                )
            scrollbar_after_drag = float(shell_attach.history_adjustment.get_value())
            if scrollbar_after_drag >= scrollbar_before_drag - 0.5:
                raise AssertionError("shell history proxy did not move with the canonical owner")

            # Exact regression: one downward wheel tick from a deep position
            # must be incremental, never an implicit return to live output.
            _wheel_down(shell_terminal)
            _drain(
                Gtk,
                until=lambda: shell_scroll_state()[1] < shell_drag_position,
            )
            shell_down_mode, shell_down_position = shell_scroll_state()
            expected_shell_down = max(0, shell_drag_position - 3)
            if shell_down_mode != 1 or shell_down_position != expected_shell_down:
                raise AssertionError(
                    "one shell wheel-down jumped or exited history: "
                    f"deep={shell_drag_position}, after={shell_down_position}, "
                    f"mode={shell_down_mode}"
                )

            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: b"x" in shell_input.read_bytes() if shell_input.exists() else False)
            _drain(Gtk, until=lambda: shell_scroll_state()[0] == 0)
            if shell_attach._history_cursor_hidden:
                raise AssertionError("shell cursor stayed hidden after returning to live input")

            _drain(Gtk, until=codex_ready.exists)
            window._focus_session(codex_record.id)
            codex_attach = window.tabs[codex_record.id].widget
            codex_terminal = codex_attach.terminal
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == codex_record.id
                    and codex_terminal.get_mapped()
                ),
            )
            _wheel_up(codex_terminal)
            _drain(
                Gtk,
                until=lambda: (
                    "transcript-open" in _events(codex_events)
                    and _events(codex_events).count("transcript-up") >= 3
                ),
            )
            before_drag_events = _events(codex_events)
            if "composer-up" in before_drag_events:
                raise AssertionError("wheel navigation reached the Codex composer")
            codex_cursor_hidden_during_history = codex_attach._history_cursor_hidden
            if not codex_cursor_hidden_during_history:
                raise AssertionError("Codex transcript left its roaming cursor visible")
            if codex_attach.scrollbar.get_adjustment() is not codex_attach.history_adjustment:
                raise AssertionError("Codex normal review scrollbar is not application-aware")

            codex_before_drag = float(codex_attach.history_adjustment.get_value())
            codex_drag_target = max(
                float(codex_attach.history_adjustment.get_lower()),
                codex_before_drag - 96.0,
            )
            codex_attach.history_adjustment.set_value(codex_drag_target)
            _drain(
                Gtk,
                until=lambda: (
                    not codex_attach._history_worker_active
                    and _events(codex_events).count("transcript-page-up") >= 4
                ),
            )
            after_drag_events = _events(codex_events)
            if any(
                event in after_drag_events
                for event in ("composer-up", "composer-page-up", "composer-page-down")
            ):
                raise AssertionError("Codex thumb drag leaked navigation into the composer")
            if "transcript-close" in after_drag_events:
                raise AssertionError("Codex thumb drag unexpectedly closed the transcript")
            codex_state = app.backend.scroll_state(codex_record)
            if codex_state is None or codex_state.depth < 90:
                raise AssertionError(f"Codex deep drag did not establish logical depth: {codex_state!r}")
            codex_deep_depth = codex_state.depth

            # Exact regression: one downward tick after a deep thumb drag must
            # move only one wheel quantum and must not toggle C-t closed.
            down_before = _events(codex_events).count("transcript-down")
            _wheel_down(codex_terminal)
            _drain(
                Gtk,
                until=lambda: _events(codex_events).count("transcript-down") >= down_before + 3,
            )
            after_down_events = _events(codex_events)
            codex_after_down = app.backend.scroll_state(codex_record)
            if codex_after_down is None or codex_after_down.depth != codex_deep_depth - 3:
                raise AssertionError(
                    "one Codex wheel-down was not incremental: "
                    f"before={codex_deep_depth}, after={codex_after_down!r}"
                )
            if "transcript-close" in after_down_events:
                raise AssertionError("one Codex wheel-down threw review back to the composer")

            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: "composer-x" in _events(codex_events))
            final_events = _events(codex_events)
            if final_events.index("transcript-close") > final_events.index("composer-x"):
                raise AssertionError("typing reached Codex before transcript view closed")
            if codex_attach._history_cursor_hidden:
                raise AssertionError("Codex composer cursor stayed hidden after live input")

            _drain(Gtk, until=native_ready.exists)
            window._focus_session(native_record.id)
            native_attach = window.tabs[native_record.id].widget
            native_terminal = native_attach.terminal
            _drain(
                Gtk,
                until=lambda: (
                    window.current_session_id() == native_record.id
                    and native_terminal.get_mapped()
                ),
            )

            def native_state() -> tuple[int, int]:
                result = app.backend._run(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        app.backend.pane_target(native_record),
                        "#{alternate_on} #{pane_in_mode}",
                    ],
                    check=False,
                )
                fields = result.stdout.strip().split()
                return (int(fields[0]), int(fields[1])) if len(fields) == 2 else (0, 0)

            _drain(Gtk, until=lambda: native_state()[0] == 1)
            _wheel_up(native_terminal)
            _drain(
                Gtk,
                until=lambda: (
                    native_input.exists()
                    and b"\x1b[<64;" in native_input.read_bytes()
                    and native_state() == (1, 0)
                ),
            )
            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: b"x" in native_input.read_bytes())
            native_alt, native_mode = native_state()

            print(
                json.dumps(
                    {
                        "ok": True,
                        "shell_copy_mode": shell_mode,
                        "shell_scroll_position": shell_position,
                        "shell_drag_scroll_position": shell_drag_position,
                        "shell_single_down_position": shell_down_position,
                        "shell_scrollbar_delta": int(
                            round(
                                scrollbar_after_drag - scrollbar_before_drag
                            )
                        ),
                        "shell_cursor_hidden_during_history": shell_cursor_hidden_during_history,
                        "shell_first_key": shell_input.read_bytes().decode("ascii"),
                        "codex_events": final_events,
                        "codex_deep_depth": codex_deep_depth,
                        "codex_single_down_depth": codex_after_down.depth if codex_after_down is not None else None,
                        "codex_cursor_hidden_during_history": codex_cursor_hidden_during_history,
                        "composer_history_was_not_touched": not any(
                            event.startswith("composer-") and event != "composer-x"
                            for event in final_events
                        ),
                        "native_alternate_screen": native_alt,
                        "native_copy_mode": native_mode,
                        "native_wheel_passthrough": b"\x1b[<64;" in native_input.read_bytes(),
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
