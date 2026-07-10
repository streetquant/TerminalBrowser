#!/usr/bin/env python3
"""Verify the polished TermDeck visual hierarchy under a real GTK display."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def _drain_gtk(Gtk, *, until=None, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.01)
    raise RuntimeError("GTK visual state did not settle before timeout")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-visual-polish-") as tmp:
        root_dir = Path(tmp)
        home = root_dir / "home"
        tmux_tmp = root_dir / "tmux"
        project = root_dir / "client-api"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        project.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        workspace = root_dir / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Visual QA",
                    "windows": [
                        {
                            "title": "Client work",
                            "tabs": [
                                {
                                    "name": "client-api",
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

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root_dir / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: bool(
                    app.windows
                    and app.windows[0].get_visible()
                    and app.windows[0].tabs
                ),
            )

            window = app.windows[0]
            main_root = window.get_child()
            notebook = window.notebook
            root_context = main_root.get_style_context()
            notebook_context = notebook.get_style_context()
            if not root_context.has_class("td-main-root"):
                raise AssertionError("main root lost its semantic visual class")
            if not notebook_context.has_class("td-terminal-notebook"):
                raise AssertionError("terminal notebook lost its polished surface class")

            margin = notebook_context.get_margin(Gtk.StateFlags.NORMAL)
            expected_margin = {"top": 10, "right": 12, "bottom": 12, "left": 12}
            actual_margin = {
                "top": int(margin.top),
                "right": int(margin.right),
                "bottom": int(margin.bottom),
                "left": int(margin.left),
            }
            if actual_margin != expected_margin:
                raise AssertionError(
                    f"unexpected terminal-surface margin: {actual_margin}"
                )

            root_allocation = main_root.get_allocation()
            notebook_allocation = notebook.get_allocation()

            header = window.get_titlebar()
            primary_buttons = [
                child
                for child in header.get_children()
                if isinstance(child, Gtk.Button)
                and child.get_style_context().has_class("suggested-action")
            ]
            if len(primary_buttons) != 1:
                raise AssertionError(
                    f"expected one circular primary action, found {len(primary_buttons)}"
                )

            css = app._chrome_css
            required_css = (
                "notebook.td-terminal-notebook tab:checked",
                "headerbar button.suggested-action.td-header-icon",
                "button:focus",
                ".td-section-description",
            )
            missing = [selector for selector in required_css if selector not in css]
            if missing:
                raise AssertionError(f"polish CSS selectors missing: {missing}")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "root_class": "td-main-root",
                        "notebook_class": "td-terminal-notebook",
                        "notebook_margin": actual_margin,
                        "root_size": f"{root_allocation.width}x{root_allocation.height}",
                        "notebook_size": (
                            f"{notebook_allocation.width}x{notebook_allocation.height}"
                        ),
                        "primary_action_count": len(primary_buttons),
                        "css_bytes": len(css.encode("utf-8")),
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
