#!/usr/bin/env python3
"""Verify Ctrl+W and Ctrl+Q preserve tmux sessions under real GTK/VTE."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from smoke_support import stream_ready


def _drain_gtk(Gtk, *, until=None, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.01)
    raise RuntimeError("GTK state did not settle before timeout")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-shortcuts-") as tmp:
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
                    "name": "Shortcut QA",
                    "windows": [
                        {
                            "title": "Work",
                            "tabs": [
                                {
                                    "name": "alpha",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                    "pinned": True,
                                },
                                {
                                    "name": "beta",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                },
                            ],
                        },
                        {
                            "title": "Other",
                            "tabs": [
                                {
                                    "name": "gamma",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                }
                            ],
                        },
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
                until=lambda: len(app.windows) == 2
                and all(
                    stream_ready(tab.widget)
                    for window in app.windows
                    for tab in window.tabs.values()
                ),
                timeout=4.0,
            )

            work = next(window for window in app.windows if window.window_title == "Work")
            other = next(window for window in app.windows if window.window_title == "Other")
            records = {record.name: record for record in app.store.list()}
            beta = records["beta"]

            beta_page = work.notebook.page_num(work.tabs[beta.id].widget)
            work.notebook.set_current_page(beta_page)
            ctrl_w = SimpleNamespace(
                keyval=Gdk.KEY_w,
                state=Gdk.ModifierType.CONTROL_MASK,
            )
            if not work.on_terminal_key_press(None, ctrl_w):
                raise AssertionError("Ctrl+W was not handled by the terminal path")
            _drain_gtk(Gtk)

            beta_after = app.store.get(beta.id)
            if beta.id in work.tabs:
                raise AssertionError("Ctrl+W did not close the active tab")
            if beta_after is None or beta_after.visible:
                raise AssertionError("Ctrl+W did not mark the active tab hidden")
            if not app.backend.session_exists(beta):
                raise AssertionError("Ctrl+W killed the underlying tmux session")

            ctrl_q = SimpleNamespace(
                keyval=Gdk.KEY_q,
                state=Gdk.ModifierType.CONTROL_MASK,
            )
            if not other.on_terminal_key_press(None, ctrl_q):
                raise AssertionError("Ctrl+Q was not handled by the terminal path")
            _drain_gtk(Gtk)

            if app.windows:
                raise AssertionError("Ctrl+Q did not close all TermDeck windows")
            surviving = {
                name: app.backend.session_exists(record)
                for name, record in records.items()
            }
            if not all(surviving.values()):
                raise AssertionError(f"Ctrl+Q killed sessions: {surviving}")
            gamma_after = app.store.get(records["gamma"].id)
            if gamma_after is None or not gamma_after.visible:
                raise AssertionError("Ctrl+Q changed restore visibility for open tabs")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "ctrl_w_closed_tab": "beta",
                        "ctrl_w_session_alive": surviving["beta"],
                        "ctrl_q_closed_windows": True,
                        "ctrl_q_sessions_alive": surviving,
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
            for window in list(Gtk.Window.list_toplevels()):
                try:
                    window.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
