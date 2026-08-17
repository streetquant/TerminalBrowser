#!/usr/bin/env python3
"""Verify text/image clipboard paste and first-key delivery into the active VTE."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import gi

from smoke_support import stream_ready

gi.require_version("GdkPixbuf", "2.0")


def _drain_gtk(Gtk, *, until=None, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.01)
    raise RuntimeError("GTK clipboard state did not settle before timeout")


def main() -> int:
    marker = "termdeck-clipboard-paste-marker"
    with tempfile.TemporaryDirectory(prefix="termdeck-clipboard-paste-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        project = root / "clipboard-project"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        project.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Clipboard QA",
                    "windows": [
                        {
                            "title": "Clipboard",
                            "tabs": [
                                {
                                    "name": "clipboard-project",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(project),
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from gi.repository import GdkPixbuf
        from termdeck.gtk_app import Gdk, Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: bool(app.windows and app.windows[0].tabs),
            )

            window = app.windows[0]
            attach = window._current_attach_terminal()
            if attach is None:
                raise AssertionError("no active terminal was available")

            clipboard = Gtk.Clipboard.get_for_display(
                attach.terminal.get_display(),
                Gdk.SELECTION_CLIPBOARD,
            )
            clipboard.set_text(marker, -1)
            if hasattr(clipboard, "store"):
                clipboard.store()
            _drain_gtk(Gtk)

            fed: list[str] = []
            attach._feed_text = lambda text: fed.append(text) or True
            event = SimpleNamespace(
                keyval=Gdk.KEY_v,
                state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
            )
            handled = window.on_terminal_key_press(attach.terminal, event)

            if not handled:
                raise AssertionError("Ctrl+Shift+V was not consumed by TermDeck")
            if fed != [marker]:
                raise AssertionError(
                    f"system clipboard text was not fed exactly once: {fed!r}"
                )

            clipboard_image = GdkPixbuf.Pixbuf.new(
                GdkPixbuf.Colorspace.RGB,
                True,
                8,
                2,
                2,
            )
            clipboard_image.fill(0x336699FF)
            clipboard.set_image(clipboard_image)
            if hasattr(clipboard, "store"):
                clipboard.store()
            _drain_gtk(Gtk)

            fed.clear()
            handled = window.on_terminal_key_press(attach.terminal, event)
            _drain_gtk(Gtk, until=lambda: bool(fed))
            if not handled:
                raise AssertionError("image Ctrl+Shift+V was not consumed by TermDeck")
            if len(fed) != 1:
                raise AssertionError(f"clipboard image path was not fed exactly once: {fed!r}")
            image_path = Path(fed[0])
            if image_path.suffix != ".png" or not image_path.is_file():
                raise AssertionError(f"clipboard image was not materialised as PNG: {fed[0]!r}")
            saved_image = GdkPixbuf.Pixbuf.new_from_file(str(image_path))
            if (saved_image.get_width(), saved_image.get_height()) != (2, 2):
                raise AssertionError("materialised clipboard image has the wrong dimensions")
            image_path.unlink()
            attach._clipboard_image_paths.discard(str(image_path))

            # The first ordinary key pressed while header chrome owns focus
            # must be replayed into VTE, not lost to the old widget.
            key_marker = root / "unfocused-first-key.txt"
            ready_marker = root / "unfocused-ready"
            record = window.current_record()
            if record is None:
                raise AssertionError("no current record was available for keyboard routing")
            _drain_gtk(Gtk, until=lambda: stream_ready(attach))
            command = (
                f": > {shlex.quote(str(ready_marker))}; "
                f"stty -icanon -echo min 1 time 0; "
                f"dd bs=1 count=1 of={shlex.quote(str(key_marker))} 2>/dev/null; "
                "stty icanon echo"
            )
            app.backend.send_text(record, command)
            _drain_gtk(Gtk, until=ready_marker.exists)

            def _find_new_tab_button(widget):
                if isinstance(widget, Gtk.Button) and widget.get_style_context().has_class("td-new-tab"):
                    return widget
                if isinstance(widget, Gtk.Container):
                    for child in widget.get_children():
                        found = _find_new_tab_button(child)
                        if found is not None:
                            return found
                return None

            chrome_button = _find_new_tab_button(window.get_titlebar())
            if chrome_button is None:
                raise AssertionError("New tab header control was not found")
            chrome_button.grab_focus()
            _drain_gtk(Gtk, until=lambda: window.get_focus() is chrome_button)
            gdk_window = window.get_window()
            if gdk_window is None:
                raise AssertionError("TermDeck window was not realised")
            xid = gdk_window.get_xid()
            subprocess.run(
                ["xdotool", "key", "--window", str(xid), "x"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _drain_gtk(
                Gtk,
                until=lambda: key_marker.exists() and key_marker.stat().st_size >= 1,
            )
            if key_marker.read_bytes() != b"x":
                raise AssertionError("the first unfocused key did not reach the terminal exactly once")
            if window.get_focus() is not attach.terminal:
                raise AssertionError("keyboard routing did not restore VTE focus")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "shortcut": "Ctrl+Shift+V",
                        "selection": "Gdk.SELECTION_CLIPBOARD",
                        "pasted_text": marker,
                        "image_path_suffix": image_path.suffix,
                        "image_dimensions": [2, 2],
                        "unfocused_first_key": "x",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for record in app.store.list(include_archived=True):
                try:
                    app.backend.terminate(record)
                except Exception:
                    pass
            for top in Gtk.Window.list_toplevels():
                try:
                    top.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
