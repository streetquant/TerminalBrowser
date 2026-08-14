#!/usr/bin/env python3
"""Verify pointer selection auto-copies without destroying the VTE highlight."""

from __future__ import annotations

import json
import os
import signal
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


def _wheel(Gtk, x: int, y: int, *, direction: str, clicks: int = 1) -> None:
    button = {"up": "4", "down": "5"}[direction]
    _move_pointer(Gtk, x, y)
    subprocess.run(
        ["xdotool", "click", "--repeat", str(clicks), button],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _drain_gtk(Gtk, timeout=0.15)


def _drag_first_line(
    Gtk,
    window,
    terminal,
    character_count: int,
    *,
    release: bool = True,
) -> tuple[int, int, int]:
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
    if release:
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
            # Relaunch only the disposable tmux attach client. The pane/shell
            # remains alive, and the replacement must not echo VTE's terminal
            # capability probes as literal ``^[...VTE(...)`` prompt garbage.
            original_attach_pid = widget.process.pid
            os.killpg(os.getpgid(original_attach_pid), signal.SIGHUP)
            _drain_gtk(
                Gtk,
                until=lambda: (
                    widget.process is not None
                    and widget.process.pid != original_attach_pid
                    and widget.process.poll() is None
                ),
            )
            _drain_gtk(Gtk, timeout=0.4)
            reattach_text = "\n".join(
                (
                    app.backend.capture(record, lines=20),
                    terminal.get_text_format(Vte.Format.TEXT) or "",
                )
            )
            probe_fragments = ("VTE(", "?61;1;21;22;28c", ">61;8401;1c")
            leaked_probes = [fragment for fragment in probe_fragments if fragment in reattach_text]
            if leaked_probes:
                raise AssertionError(
                    f"reattached terminal displayed capability probes: {leaked_probes!r}"
                )
            app.backend.send_text(
                record,
                (
                    "i=0; while [ $i -lt 300 ]; do "
                    "printf 'TERMDECK_SCROLLBACK_%03d________________________________\\n' \"$i\"; "
                    "i=$((i + 1)); done; "
                    f"clear; printf '%s\\n' {MARKER}"
                ),
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

            # This must remain one native VTE interaction, exactly like
            # Terminator: wheel motion may reveal earlier/later rows but must
            # not replace the terminal with a review widget, clear the range,
            # or consume only one wheel direction. Use physical X11 input so
            # event-window routing and active pointer grabs are exercised too.
            adjustment = terminal.get_vadjustment()
            lower = float(adjustment.get_lower())
            upper = float(adjustment.get_upper()) - float(adjustment.get_page_size())
            before_up = float(adjustment.get_value())
            if upper <= lower + 0.5:
                raise AssertionError(
                    "TermDeck VTE has no native scrollback; continuous browser-like "
                    "selection cannot be implemented by redrawing tmux history"
                )
            pointer_x = (drag_coordinates[0] + drag_coordinates[1]) // 2
            pointer_y = drag_coordinates[2]
            _wheel(Gtk, pointer_x, pointer_y, direction="up", clicks=3)
            after_up = float(adjustment.get_value())
            if after_up >= before_up - 0.5:
                raise AssertionError(
                    f"physical wheel-up did not move native history: {before_up} -> {after_up}"
                )
            if getattr(widget, "_selection_review_active", False):
                raise AssertionError("selection wheel switched away from the native VTE")
            if not terminal.get_has_selection():
                raise AssertionError("physical wheel-up destroyed the VTE selection")
            if terminal.get_text_selected(Vte.Format.TEXT) != MARKER:
                raise AssertionError("physical wheel-up changed the selected payload")

            _wheel(Gtk, pointer_x, pointer_y, direction="down", clicks=2)
            after_down = float(adjustment.get_value())
            if after_down <= after_up + 0.5:
                raise AssertionError(
                    f"physical wheel-down did not move native history: {after_up} -> {after_down}"
                )
            if not terminal.get_has_selection():
                raise AssertionError("physical wheel-down destroyed the VTE selection")
            if terminal.get_text_selected(Vte.Format.TEXT) != MARKER:
                raise AssertionError("physical wheel-down changed the selected payload")
            if not terminal.get_visible() or not terminal.get_mapped():
                raise AssertionError("selection scrolling blanked or unmapped the terminal")

            # Exercise the stricter browser gesture: keep button 1 depressed,
            # wheel to reveal older rows, move the endpoint onto a newly
            # visible row, and only then release. The result must be one
            # continuous native VTE selection containing both the original
            # marker and the newly exposed tmux history.
            terminal.unselect_all()
            held_live_edge = max(
                float(adjustment.get_lower()),
                float(adjustment.get_upper()) - float(adjustment.get_page_size()),
            )
            adjustment.set_value(held_live_edge)
            _drain_gtk(Gtk, timeout=0.2)
            held_drag = _drag_first_line(
                Gtk,
                window,
                terminal,
                len(MARKER),
                release=False,
            )
            try:
                _drain_gtk(Gtk, timeout=0.2)
                held_initial = terminal.get_text_selected(Vte.Format.TEXT) or ""
                if held_initial != MARKER:
                    raise AssertionError(
                        "held drag did not start on the live marker: "
                        f"selection={held_initial!r}, edge={held_live_edge}, "
                        f"value={float(adjustment.get_value())}"
                    )
                held_before = float(adjustment.get_value())
                _wheel(
                    Gtk,
                    held_drag[1],
                    held_drag[2],
                    direction="up",
                    clicks=4,
                )
                held_after_up = float(adjustment.get_value())
                if held_after_up >= held_before - 0.5:
                    raise AssertionError(
                        "wheel-up did not move while the selection button was held"
                    )
                _move_pointer(Gtk, held_drag[0] + 8, held_drag[2])
            finally:
                subprocess.run(
                    ["xdotool", "mouseup", "1"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            _drain_gtk(Gtk, timeout=0.8)
            extended = terminal.get_text_selected(Vte.Format.TEXT) or ""
            if MARKER not in extended or "TERMDECK_SCROLLBACK" not in extended:
                raise AssertionError(
                    "held drag did not extend into newly revealed history: "
                    f"selection={extended!r}"
                )
            if not terminal.get_has_selection():
                raise AssertionError("held wheel/drag lost the native VTE selection")
            _wheel(
                Gtk,
                held_drag[0] + 8,
                held_drag[2],
                direction="down",
                clicks=2,
            )
            if float(adjustment.get_value()) <= held_after_up + 0.5:
                raise AssertionError("wheel-down did not move the extended selection")
            if not terminal.get_has_selection():
                raise AssertionError("wheel-down cleared the extended native selection")

            # Releasing the extended drag must replace the earlier one-line
            # clipboard payload with the complete extended range. Clearing the
            # highlight afterward must not clear that durable system copy.
            _drain_gtk(
                Gtk,
                until=lambda: widget._last_copied_text == extended,
                timeout=2.0,
            )
            terminal.unselect_all()
            _drain_gtk(Gtk)
            if clipboard.wait_for_text() != extended:
                raise AssertionError(
                    "extended system clipboard was lost after selection cleared"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "clean_attach_relaunch": True,
                        "selected_text": selected,
                        "selection_stays_visible": True,
                        "clipboard_exact": True,
                        "primary_exact": True,
                        "native_scroll_up": before_up - after_up,
                        "native_scroll_down": after_down - after_up,
                        "selection_survives_bidirectional_scroll": True,
                        "held_drag_extends_into_new_history": True,
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
