#!/usr/bin/env python3
"""Verify text/image clipboard paste and first-key delivery into the active VTE."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import gi

from smoke_support import require_private_validation_runtime, stream_ready

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
    tempfile_root = Path(tempfile.gettempdir()).resolve()
    runtime_base = require_private_validation_runtime()
    with tempfile.TemporaryDirectory(
        # Keep the tmux socket path below the AF_UNIX pathname limit even
        # when the runner's private runtime is nested under a task directory.
        prefix="tdcb-",
        dir=runtime_base,
    ) as tmux_tmp_name, tempfile.TemporaryDirectory(
        prefix="termdeck-clipboard-paste-"
    ) as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = Path(tmux_tmp_name)
        socket_path = tmux_tmp / f"tmux-{os.geteuid()}" / "default"
        if len(os.fsencode(socket_path)) >= 108:
            raise AssertionError(
                f"tmux socket path exceeds the AF_UNIX budget: {socket_path}"
            )
        project = root / "clipboard-project"
        home.mkdir()
        tmux_tmp.chmod(0o700)
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

            image_feed_errors: list[Exception] = []
            observed_image_path: Path | None = None
            observed_image_dimensions: tuple[int, int] | None = None

            def _capture_image_feed(text: str) -> bool:
                nonlocal observed_image_dimensions, observed_image_path
                fed.append(text)
                try:
                    image_tokens = shlex.split(text)
                    if len(image_tokens) != 1:
                        raise AssertionError(f"clipboard image path was not shell-safe: {text!r}")
                    candidate = Path(image_tokens[0])
                    if candidate.suffix != ".png" or not candidate.is_absolute():
                        raise AssertionError(
                            f"clipboard image path was not an absolute PNG path: {text!r}"
                        )
                    image_metadata = candidate.lstat()
                    image_parent = candidate.parent.resolve(strict=True)
                    if (
                        image_parent != tempfile_root
                        or not stat.S_ISREG(image_metadata.st_mode)
                        or image_metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(image_metadata.st_mode) != 0o600
                    ):
                        raise AssertionError(
                            f"clipboard image was not securely materialised under {tempfile_root}: "
                            f"{text!r}"
                        )
                    saved_image = GdkPixbuf.Pixbuf.new_from_file(str(candidate))
                    observed_image_dimensions = (
                        saved_image.get_width(),
                        saved_image.get_height(),
                    )
                    if observed_image_dimensions != (2, 2):
                        raise AssertionError("materialised clipboard image has the wrong dimensions")
                    observed_image_path = candidate
                except Exception as exc:
                    image_feed_errors.append(exc)
                return True

            attach._feed_text = _capture_image_feed
            handled = window.on_terminal_key_press(attach.terminal, event)
            _drain_gtk(Gtk, until=lambda: bool(fed))
            if not handled:
                raise AssertionError("image Ctrl+Shift+V was not consumed by TermDeck")
            if len(fed) != 1:
                raise AssertionError(f"clipboard image path was not fed exactly once: {fed!r}")
            if image_feed_errors:
                raise AssertionError("clipboard image validation failed during feeding") from image_feed_errors[0]
            if observed_image_path is None or observed_image_dimensions != (2, 2):
                raise AssertionError("clipboard image was not validated during feeding")
            image_path = observed_image_path
            attach._cleanup_clipboard_images()
            if image_path.exists() or attach._clipboard_image_paths:
                raise AssertionError("clipboard image was not cleaned up after use")

            # The first ordinary key pressed while header chrome owns focus
            # must be replayed into VTE, not lost to the old widget.
            key_marker = root / "unfocused-first-key.txt"
            ready_marker = root / "unfocused-ready"
            key_reader = root / "read_one_key.py"
            key_reader.write_text(
                """from __future__ import annotations
import os
import sys
import termios
from pathlib import Path

ready = Path(sys.argv[1])
output = Path(sys.argv[2])
fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~(termios.ICANON | termios.ECHO)
raw[6][termios.VMIN] = 1
raw[6][termios.VTIME] = 0
try:
    termios.tcsetattr(fd, termios.TCSANOW, raw)
    ready.touch()
    output.write_bytes(os.read(fd, 1))
finally:
    termios.tcsetattr(fd, termios.TCSANOW, original)
""",
                encoding="utf-8",
            )
            # Clipboard ownership can briefly disturb the notebook's current
            # page while GTK drains the portal callbacks. Resolve the session
            # from the already-selected terminal widget and restore that page
            # before routing the first-key assertion.
            attach_session_id = next(
                (
                    session_id
                    for session_id, tab in window.tabs.items()
                    if tab.widget is attach
                ),
                None,
            )
            if attach_session_id is None:
                raise AssertionError("clipboard terminal disappeared before keyboard routing")
            attach_page = window.notebook.page_num(attach)
            if attach_page < 0:
                raise AssertionError("clipboard terminal page disappeared before keyboard routing")
            if window.current_session_id() != attach_session_id:
                window.notebook.set_current_page(attach_page)
                _drain_gtk(
                    Gtk,
                    until=lambda: window.current_session_id() == attach_session_id,
                )
            record = app.store.get(attach_session_id)
            if record is None:
                raise AssertionError("clipboard terminal record disappeared before keyboard routing")
            _drain_gtk(Gtk, until=lambda: stream_ready(attach))
            command = shlex.join(
                [
                    sys.executable,
                    str(key_reader),
                    str(ready_marker),
                    str(key_marker),
                ]
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
