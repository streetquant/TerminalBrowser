#!/usr/bin/env python3
"""Verify real TermDeck/tmux wheel scrolling, including Codex transcript routing."""

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


def _wheel_up(terminal) -> None:
    _ok, x, y = terminal.get_window().get_origin()
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(x + 80), str(y + 80), "click", "4"],
        check=True,
        timeout=5,
    )


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
        codex_executable = root / "codex"
        codex_executable.symlink_to(sys.executable)

        shell_command = shlex.join(
            [sys.executable, str(shell_worker), str(shell_ready), str(shell_input)]
        )
        codex_command = shlex.join(
            [str(codex_executable), str(codex_simulator), str(codex_ready), str(codex_events)]
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
                    until=lambda: bool(app.windows and len(app.windows[0].tabs) == 2),
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

            _drain(Gtk, until=shell_ready.exists)
            window._focus_session(shell_record.id)
            shell_attach = window.tabs[shell_record.id].widget
            shell_terminal = shell_attach.terminal
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

            _drain(Gtk, until=lambda: shell_scroll_state()[1] >= 3)
            shell_mode, shell_position = shell_scroll_state()
            shell_cursor_hidden_during_history = shell_attach._history_cursor_hidden
            if not shell_cursor_hidden_during_history:
                raise AssertionError("shell history left a roaming terminal cursor visible")
            scrollbar_before_drag = float(shell_attach.scroll_adjustment.get_value())
            shell_attach.scroll_adjustment.set_value(scrollbar_before_drag - 30.0)
            _drain(Gtk, until=lambda: shell_scroll_state()[1] >= shell_position + 30)
            _shell_drag_mode, shell_drag_position = shell_scroll_state()
            scrollbar_after_drag = float(shell_attach.scroll_adjustment.get_value())
            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: b"x" in shell_input.read_bytes() if shell_input.exists() else False)
            _drain(Gtk, until=lambda: shell_scroll_state()[0] == 0)
            if shell_attach._history_cursor_hidden:
                raise AssertionError("shell cursor stayed hidden after returning to live input")

            _drain(Gtk, until=codex_ready.exists)
            window._focus_session(codex_record.id)
            codex_attach = window.tabs[codex_record.id].widget
            codex_terminal = codex_attach.terminal
            _wheel_up(codex_terminal)
            _drain(
                Gtk,
                until=lambda: (
                    "transcript-open" in _events(codex_events)
                    and _events(codex_events).count("transcript-up") >= 3
                ),
            )
            before_input = _events(codex_events)
            if "composer-up" in before_input:
                raise AssertionError("wheel navigation reached the Codex composer")
            codex_cursor_hidden_during_history = codex_attach._history_cursor_hidden
            if not codex_cursor_hidden_during_history:
                raise AssertionError("Codex transcript left its roaming cursor visible")

            subprocess.run(["xdotool", "key", "x"], check=True, timeout=5)
            _drain(Gtk, until=lambda: "composer-x" in _events(codex_events))
            final_events = _events(codex_events)
            if final_events.index("transcript-close") > final_events.index("composer-x"):
                raise AssertionError("typing reached Codex before transcript view closed")
            if codex_attach._history_cursor_hidden:
                raise AssertionError("Codex composer cursor stayed hidden after live input")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "shell_copy_mode": shell_mode,
                        "shell_scroll_position": shell_position,
                        "shell_drag_scroll_position": shell_drag_position,
                        "shell_scrollbar_delta": int(
                            round(
                                scrollbar_after_drag - scrollbar_before_drag
                            )
                        ),
                        "shell_cursor_hidden_during_history": shell_cursor_hidden_during_history,
                        "shell_first_key": shell_input.read_bytes().decode("ascii"),
                        "codex_events": final_events,
                        "codex_cursor_hidden_during_history": codex_cursor_hidden_during_history,
                        "composer_history_was_not_touched": "composer-up" not in final_events,
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
