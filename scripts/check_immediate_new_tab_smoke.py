#!/usr/bin/env python3
"""Verify Ctrl+T opens a shell immediately in the active pane's live cwd."""

from __future__ import annotations

import json
import os
import shlex
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
    raise RuntimeError("GTK state did not settle before timeout")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-new-tab-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        starting = root / "starting"
        live = root / "live-work"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        starting.mkdir()
        live.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Immediate tab QA",
                    "windows": [
                        {
                            "title": "Work",
                            "tabs": [
                                {
                                    "name": "starting",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(starting),
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
                until=lambda: bool(
                    app.windows
                    and len(app.windows[0].tabs) == 1
                    and next(iter(app.windows[0].tabs.values())).widget.process is not None
                ),
            )

            window = app.windows[0]
            original = next(iter(window.tabs.values())).record
            app.backend.send_text(
                original,
                f"cd -- {shlex.quote(str(live))}",
                enter=True,
            )
            _drain_gtk(
                Gtk,
                until=lambda: app.backend.current_directory(original) == str(live.resolve()),
            )

            before_ids = {record.id for record in app.store.list()}
            ctrl_t = SimpleNamespace(
                keyval=Gdk.KEY_t,
                state=Gdk.ModifierType.CONTROL_MASK,
            )
            started = time.monotonic()
            if not window.on_terminal_key_press(None, ctrl_t):
                raise AssertionError("Ctrl+T was not handled by the terminal path")
            elapsed_ms = (time.monotonic() - started) * 1000

            _drain_gtk(
                Gtk,
                until=lambda: len(window.tabs) == 2
                and len(app.store.list()) == 2
                and all(tab.widget.process is not None for tab in window.tabs.values()),
            )
            created = [record for record in app.store.list() if record.id not in before_ids]
            if len(created) != 1:
                raise AssertionError(f"expected one new tab record, found {len(created)}")
            new_record = created[0]
            if Path(new_record.cwd) != live.resolve():
                raise AssertionError(
                    f"new tab used {new_record.cwd!r}, expected live cwd {str(live.resolve())!r}"
                )
            if new_record.name != live.name:
                raise AssertionError(
                    f"new tab name {new_record.name!r} did not use directory basename {live.name!r}"
                )
            if not app.backend.session_exists(new_record):
                raise AssertionError("new tab tmux session was not started")
            chooser_windows = [
                top
                for top in Gtk.Window.list_toplevels()
                if "choose directory" in (top.get_title() or "").casefold()
            ]
            if chooser_windows:
                raise AssertionError("a directory chooser appeared during Ctrl+T")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "shortcut": "Ctrl+T",
                        "stored_start_directory": str(starting.resolve()),
                        "live_active_directory": str(live.resolve()),
                        "new_tab_directory": new_record.cwd,
                        "new_tab_name": new_record.name,
                        "chooser_windows": len(chooser_windows),
                        "handler_elapsed_ms": round(elapsed_ms, 2),
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
