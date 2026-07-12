#!/usr/bin/env python3
"""Verify pointer selection auto-copies without destroying the VTE highlight."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


MARKER = "TERMDECK_SELECT_COPY_24680"


def _drain_gtk(Gtk, *, until=None, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return
        time.sleep(0.02)
    if until is not None:
        raise RuntimeError("GTK state did not settle before timeout")


def _move_pointer(Gtk, x: int, y: int) -> None:
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(x), str(y)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _drain_gtk(Gtk, timeout=0.04)


def _drag_first_line(Gtk, window, terminal, character_count: int) -> tuple[int, int, int]:
    translated = terminal.translate_coordinates(window, 0, 0)
    if translated is None or window.get_window() is None:
        raise AssertionError("terminal was not realised for pointer selection")
    terminal_x, terminal_y = int(translated[-2]), int(translated[-1])
    origin = window.get_window().get_origin()
    origin_x, origin_y = int(origin[-2]), int(origin[-1])
    char_width = int(terminal.get_char_width())
    char_height = int(terminal.get_char_height())
    start_x = origin_x + terminal_x + max(2, char_width // 2)
    end_x = origin_x + terminal_x + (char_width * character_count)
    y = origin_y + terminal_y + max(2, char_height // 2)

    _move_pointer(Gtk, start_x, y)
    subprocess.run(
        ["xdotool", "mousedown", "1"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for x in range(start_x + 20, end_x, 20):
        _move_pointer(Gtk, x, y)
    _move_pointer(Gtk, end_x, y)
    subprocess.run(
        ["xdotool", "mouseup", "1"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return start_x, end_x, y


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-select-copy-smoke-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Select copy QA",
                    "windows": [
                        {
                            "title": "Clipboard",
                            "tabs": [
                                {
                                    "name": "selection",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Import only after HOME/TMUX_TMPDIR are isolated because package-level
        # settings paths are resolved during import.
        from termdeck.gtk_app import Gdk, Gtk, TermDeckApplication, Vte

        app = TermDeckApplication(
            db_path=root / "sessions.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: bool(app.windows))
            window = app.windows[0]
            window.move(0, 0)
            window.resize(900, 520)
            _drain_gtk(Gtk)

            record = app.store.list()[0]
            widget = window.tabs[record.id].widget
            terminal = widget.terminal
            _drain_gtk(
                Gtk,
                until=lambda: widget.process is not None and widget.process.poll() is None,
            )
            app.backend.send_text(
                record,
                f"clear; printf '%s\\n' {MARKER}",
                enter=True,
            )
            _drain_gtk(
                Gtk,
                until=lambda: MARKER in app.backend.capture(record, lines=5),
            )
            _drain_gtk(
                Gtk,
                until=lambda: MARKER in (terminal.get_text_format(Vte.Format.TEXT) or ""),
            )

            pointer_events = []
            terminal.connect(
                "button-press-event",
                lambda _terminal, event: pointer_events.append(
                    ("press", int(event.button), round(event.x), round(event.y))
                ) or False,
            )
            terminal.connect(
                "button-release-event",
                lambda _terminal, event: pointer_events.append(
                    ("release", int(event.button), round(event.x), round(event.y))
                ) or False,
            )
            drag_coordinates = _drag_first_line(Gtk, window, terminal, len(MARKER))
            _drain_gtk(Gtk, timeout=0.8)
            if widget._last_copied_text != MARKER:
                raise AssertionError(
                    "pointer selection did not auto-copy the expected row: "
                    f"copied={widget._last_copied_text!r}, "
                    f"selected={terminal.get_text_selected(Vte.Format.TEXT)!r}, "
                    f"capture={app.backend.capture(record, lines=5)!r}, "
                    f"terminal={terminal.get_text_format(Vte.Format.TEXT)!r}, "
                    f"drag={drag_coordinates!r}, events={pointer_events!r}"
                )

            selected = terminal.get_text_selected(Vte.Format.TEXT)
            if selected != MARKER:
                raise AssertionError(f"VTE selected unexpected text: {selected!r}")
            if not terminal.get_has_selection():
                raise AssertionError("auto-copy destroyed the visible terminal selection")
            clipboard = Gtk.Clipboard.get_for_display(
                terminal.get_display(),
                Gdk.SELECTION_CLIPBOARD,
            )
            primary = Gtk.Clipboard.get_for_display(
                terminal.get_display(),
                Gdk.SELECTION_PRIMARY,
            )
            clipboard_text = clipboard.wait_for_text()
            primary_text = primary.wait_for_text()
            if clipboard_text != MARKER:
                raise AssertionError(f"CLIPBOARD did not receive selection: {clipboard_text!r}")
            if primary_text != MARKER:
                raise AssertionError(f"PRIMARY did not receive selection: {primary_text!r}")

            # Clearing the highlight must not clear the durable system copy.
            terminal.unselect_all()
            _drain_gtk(Gtk)
            if clipboard.wait_for_text() != MARKER:
                raise AssertionError("system clipboard was lost after selection cleared")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "selected_text": selected,
                        "selection_stays_visible": True,
                        "clipboard_exact": True,
                        "primary_exact": True,
                        "clipboard_survives_unselect": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for row in app.store.list():
                try:
                    app.backend.terminate(row)
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
