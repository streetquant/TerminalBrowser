#!/usr/bin/env python3
"""Verify TermDeck's minimal mapped HeaderBar under a real GTK/X11 display."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def _drain_gtk(Gtk, *, until=None, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    raise RuntimeError("GTK header state did not settle before timeout")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-header-smoke-") as tmp:
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
                    "name": "Header QA",
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

        # Import after HOME and TMUX_TMPDIR are isolated because settings paths
        # and the hidden backend are configured when the package is imported.
        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: bool(app.windows))

            window = app.windows[0]
            header = window.get_titlebar()
            if not isinstance(header, Gtk.HeaderBar):
                raise AssertionError("main window does not use a GTK HeaderBar")

            custom_title = header.get_custom_title()
            if not isinstance(custom_title, Gtk.Label):
                raise AssertionError("HeaderBar custom title must be one label")
            if custom_title.get_text() != "Backend":
                raise AssertionError(
                    f"in-app title must be the window name only: {custom_title.get_text()!r}"
                )
            if window.get_title() != "TermDeck · Backend":
                raise AssertionError(
                    f"native title must retain the app prefix: {window.get_title()!r}"
                )

            actions: dict[str, Gtk.Widget] = {}
            for child in header.get_children():
                if not isinstance(child, Gtk.Button):
                    continue
                try:
                    name = child.get_accessible().get_name() or ""
                except Exception:
                    name = ""
                if name:
                    actions[name] = child

            if set(actions) != {"New tab", "More actions"}:
                raise AssertionError(
                    f"HeaderBar exposed unexpected actions: {sorted(actions)}"
                )
            if actions["New tab"].get_tooltip_text() != "New tab · Ctrl+T · opens in the active terminal directory":
                raise AssertionError("New tab lost its directory-based tooltip")

            more = actions["More actions"]
            if not isinstance(more, Gtk.MenuButton):
                raise AssertionError("More actions must be a GTK MenuButton")
            popup = more.get_popup()
            if not isinstance(popup, Gtk.Menu):
                raise AssertionError("More actions has no GTK menu")
            menu_labels = [
                item.get_label()
                for item in popup.get_children()
                if isinstance(item, Gtk.MenuItem)
            ]
            expected_menu = [
                "Rename window",
                "New TermDeck window",
                "Search tabs",
                "Command palette",
                "Reopen closed tab",
                "Show/hide Tabs pane",
                "Tab manager · per-tab details",
                "Window overview · window totals",
                "Keyboard shortcuts",
                "Terminal appearance",
                "Quit TermDeck",
            ]
            if menu_labels != expected_menu:
                raise AssertionError(f"unexpected ordered More menu: {menu_labels}")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "in_app_title": custom_title.get_text(),
                        "native_title": window.get_title(),
                        "visible_actions": sorted(actions),
                        "more_actions": menu_labels,
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
