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
            attach = window._current_attach_terminal()
            if attach is None:
                raise AssertionError("active terminal is missing")
            main_root = window.get_child()
            notebook = window.notebook
            root_context = main_root.get_style_context()
            notebook_context = notebook.get_style_context()
            if not root_context.has_class("td-main-root"):
                raise AssertionError("main root lost its semantic visual class")
            if not notebook_context.has_class("td-terminal-notebook"):
                raise AssertionError("terminal notebook lost its polished surface class")
            if attach.terminal_row.get_children() != [attach.terminal, attach.scrollbar]:
                raise AssertionError("terminal scrollbar is not placed on VTE's right edge")
            if attach.scrollbar.get_adjustment() == attach.terminal.get_vadjustment():
                raise AssertionError(
                    "terminal scrollbar still uses VTE's empty tmux alternate-screen range"
                )
            if not attach.scrollbar.get_visible():
                raise AssertionError("terminal scrollbar is not visible")
            if not attach.scrollbar.get_style_context().has_class("td-terminal-scrollbar"):
                raise AssertionError("terminal scrollbar lost its Terminator-style class")

            _drain_gtk(
                Gtk,
                until=lambda: (
                    attach.scrollbar.get_allocated_width() > 0
                    and attach.scrollbar.get_allocated_height() > 100
                ),
            )
            scrollbar_allocation = attach.scrollbar.get_allocation()
            scrollbar_adjustment = attach.scrollbar.get_adjustment()
            scrollbar_range = {
                "lower": float(scrollbar_adjustment.get_lower()),
                "upper": float(scrollbar_adjustment.get_upper()),
                "page_size": float(scrollbar_adjustment.get_page_size()),
                "value": float(scrollbar_adjustment.get_value()),
            }
            if scrollbar_range["upper"] <= scrollbar_range["page_size"]:
                raise AssertionError(
                    "terminal scrollbar has no usable range, so GTK cannot draw its thumb: "
                    f"{scrollbar_range}"
                )
            if scrollbar_allocation.width < 14:
                raise AssertionError(
                    "terminal scrollbar is present in the widget tree but visually collapsed: "
                    f"{scrollbar_allocation.width}x{scrollbar_allocation.height}"
                )

            margin = notebook_context.get_margin(Gtk.StateFlags.NORMAL)
            expected_margin = {"top": 6, "right": 8, "bottom": 8, "left": 8}
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

            _drain_gtk(
                Gtk,
                until=lambda: (
                    notebook.get_allocated_width() > 100
                    and notebook.get_allocated_height() > 100
                ),
            )
            root_allocation = main_root.get_allocation()
            notebook_allocation = notebook.get_allocation()
            if (
                notebook_allocation.width < root_allocation.width * 0.9
                or notebook_allocation.height < root_allocation.height * 0.9
            ):
                raise AssertionError(
                    "hidden Tabs pane left the terminal surface collapsed: "
                    f"root={root_allocation.width}x{root_allocation.height}, "
                    f"notebook={notebook_allocation.width}x{notebook_allocation.height}"
                )

            header = window.get_titlebar()
            new_tab_buttons = [
                child
                for child in header.get_children()
                if isinstance(child, Gtk.Button)
                and child.get_style_context().has_class("td-new-tab")
            ]
            if len(new_tab_buttons) != 1:
                raise AssertionError(
                    f"expected one compact new-tab action, found {len(new_tab_buttons)}"
                )

            css = app._chrome_css
            required_css = (
                "notebook.td-terminal-notebook tab:checked",
                "headerbar button.td-new-tab.td-header-icon",
                "button:focus",
                ".td-section-description",
                "scrollbar.td-terminal-scrollbar",
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
                        "new_tab_action_count": len(new_tab_buttons),
                        "terminal_scrollbar_visible": True,
                        "terminal_scrollbar_size": (
                            f"{scrollbar_allocation.width}x{scrollbar_allocation.height}"
                        ),
                        "terminal_scrollbar_range": scrollbar_range,
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
