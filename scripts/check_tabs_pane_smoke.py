#!/usr/bin/env python3
"""Verify the terminal-first Tabs-pane lifecycle under a real GTK display."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def _drain_gtk(Gtk, *, until=None, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    raise RuntimeError("GTK state did not settle before timeout")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-tabs-pane-smoke-") as tmp:
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
                    "name": "Pane QA",
                    "windows": [
                        {
                            "title": "Backend",
                            "tabs": [
                                {
                                    "name": "backend",
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

        # Import only after HOME/TMUX_TMPDIR are isolated because settings paths
        # are computed when the package is imported.
        from termdeck.gtk_app import Gdk, Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: bool(app.windows))

            first = app.windows[0]
            if first.tabs_pane_visible or first.side.get_visible():
                raise AssertionError("Tabs pane must be hidden on a fresh/default launch")

            f9 = SimpleNamespace(keyval=Gdk.KEY_F9, state=0)
            if not first.on_key_press(first, f9):
                raise AssertionError("F9 did not handle the Tabs-pane toggle")
            _drain_gtk(Gtk)
            if not first.tabs_pane_visible or not first.side.get_visible():
                raise AssertionError("F9 did not show the Tabs pane")

            payload = json.loads(app.settings.path.read_text(encoding="utf-8"))
            if payload.get("appearance", {}).get("tabs_pane_visible") is not True:
                raise AssertionError("shown Tabs-pane state was not persisted")

            second = app.create_named_window("Research", open_initial_tab=False)
            _drain_gtk(Gtk)
            if not second.tabs_pane_visible or not second.side.get_visible():
                raise AssertionError("new windows did not inherit visible Tabs-pane state")

            if not first.on_key_press(first, f9):
                raise AssertionError("second F9 toggle was not handled")
            _drain_gtk(Gtk)
            if first.side.get_visible() or second.side.get_visible():
                raise AssertionError("Tabs-pane hiding did not synchronise across windows")

            payload = json.loads(app.settings.path.read_text(encoding="utf-8"))
            appearance = payload.get("appearance", {})
            if appearance.get("tabs_pane_visible") is not False:
                raise AssertionError("hidden Tabs-pane state was not persisted")
            if "focus_mode" in appearance or "focus" in appearance:
                raise AssertionError("legacy focus keys remained after canonical save")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "default_hidden": True,
                        "f9_toggle": True,
                        "persisted": True,
                        "new_window_inherits": True,
                        "all_windows_synchronise": True,
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
