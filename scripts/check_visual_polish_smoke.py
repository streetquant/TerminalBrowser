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
    from smoke_support import require_private_validation_runtime

    # tmux's AF_UNIX socket includes the fixture root. Keep it inside the
    # admitted disposable runtime and below the kernel's pathname limit.
    with tempfile.TemporaryDirectory(prefix="vp-", dir=require_private_validation_runtime()) as tmp:
        root_dir = Path(tmp)
        home = root_dir / "home"
        tmux_tmp = root_dir / "tmux"
        project = root_dir / "client-api"
        runtime_dir = root_dir / "runtime"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        project.mkdir()
        runtime_dir.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"
        os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)

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
                                    "command": "printf 'T1000_VISIBLE_TERMINAL\\n'; exec sleep 60",
                                    "cwd": str(project),
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from termdeck.gtk_app import Gtk, Vte, TermDeckApplication

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

            def terminal_text() -> str:
                return attach.terminal.get_text_format(Vte.Format.TEXT) or ""

            try:
                _drain_gtk(Gtk, until=lambda: "T1000_VISIBLE_TERMINAL" in terminal_text(), timeout=12)
            except RuntimeError as exc:
                raise RuntimeError(f"visual fixture terminal did not render: text={terminal_text()!r}; records={app.store.list()!r}") from exc
            for width, height in ((640, 480), (980, 620), (640, 480)):
                previous_grid = (attach.terminal.get_column_count(), attach.terminal.get_row_count())
                window.resize(width, height)
                _drain_gtk(Gtk, until=lambda: (
                    window.get_size() == (width, height)
                    and (attach.terminal.get_column_count(), attach.terminal.get_row_count()) != previous_grid
                    and not window._terminal_layout_sync_source_id
                    and not attach._resize_sync_source_id
                ))
                _drain_gtk(Gtk, until=lambda: "T1000_VISIBLE_TERMINAL" in terminal_text(), timeout=8)
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
            if attach.native_adjustment is not attach.terminal.get_vadjustment():
                raise AssertionError("terminal lost its native VTE adjustment identity")
            if attach.history_adjustment is attach.native_adjustment:
                raise AssertionError("application and VTE selection histories were conflated")
            if attach.scrollbar.get_adjustment() is not attach.history_adjustment:
                raise AssertionError(
                    "terminal scrollbar is not bound to application-aware history"
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
            if (
                scrollbar_range["upper"] < scrollbar_range["lower"]
                or scrollbar_range["page_size"] < 0
                or not (
                    scrollbar_range["lower"]
                    <= scrollbar_range["value"]
                    <= max(scrollbar_range["lower"], scrollbar_range["upper"])
                )
            ):
                raise AssertionError(
                    f"terminal scrollbar exposes an incoherent history range: {scrollbar_range}"
                )
            if scrollbar_allocation.width < 14:
                raise AssertionError(
                    "terminal scrollbar is present in the widget tree but visually collapsed: "
                    f"{scrollbar_allocation.width}x{scrollbar_allocation.height}"
                )

            margin = notebook_context.get_margin(Gtk.StateFlags.NORMAL)
            # Current browser-like chrome docks the terminal notebook edge to
            # edge; the old floating-card margin was intentionally removed.
            expected_margin = {"top": 0, "right": 0, "bottom": 0, "left": 0}
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
                        "compact_size": list(window.get_size()),
                        "visible_terminal_marker": "T1000_VISIBLE_TERMINAL" in terminal_text(),
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
