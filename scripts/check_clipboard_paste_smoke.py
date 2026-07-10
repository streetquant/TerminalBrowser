#!/usr/bin/env python3
"""Verify Ctrl+Shift+V pastes the system clipboard into the active VTE."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


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

            print(
                json.dumps(
                    {
                        "ok": True,
                        "shortcut": "Ctrl+Shift+V",
                        "selection": "Gdk.SELECTION_CLIPBOARD",
                        "pasted_text": marker,
                        "feed_count": len(fed),
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
