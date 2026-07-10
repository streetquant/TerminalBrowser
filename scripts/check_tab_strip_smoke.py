#!/usr/bin/env python3
"""Verify that notebook tabs show only their exact names and close controls."""

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
    raise RuntimeError("GTK state did not settle before timeout")


def _tab_parts(Gtk, window, page_index: int = 0):
    page = window.notebook.get_nth_page(page_index)
    if page is None:
        raise AssertionError(f"missing notebook page {page_index}")
    tab_label = window.notebook.get_tab_label(page)
    if not isinstance(tab_label, Gtk.Box):
        raise AssertionError(f"unexpected tab-label widget: {type(tab_label).__name__}")
    labels = [child for child in tab_label.get_children() if isinstance(child, Gtk.Label)]
    buttons = [child for child in tab_label.get_children() if isinstance(child, Gtk.Button)]
    if len(labels) != 1 or len(buttons) != 1:
        raise AssertionError(
            f"tab must contain one label and one close button; got {len(labels)} labels and {len(buttons)} buttons"
        )
    return labels[0], buttons[0]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-tab-strip-smoke-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        workdir = root / "client-api"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        workdir.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Tab QA",
                    "windows": [
                        {
                            "title": "Backend",
                            "tabs": [
                                {
                                    "name": "client-api",
                                    "tag": "ai",
                                    "command": "{shell}",
                                    "cwd": str(workdir),
                                    "pinned": True,
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
        from termdeck.gtk_app import Gtk, Pango, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: bool(app.windows and app.windows[0].tabs))

            window = app.windows[0]
            label, close = _tab_parts(Gtk, window)
            if label.get_text() != "client-api":
                raise AssertionError(f"decorated tab label leaked into strip: {label.get_text()!r}")
            if label.get_ellipsize() != Pango.EllipsizeMode.END:
                raise AssertionError("tab label is not ellipsized at the end")
            if label.get_max_width_chars() != 24:
                raise AssertionError("tab label width cap changed")
            if close.get_label() != "×":
                raise AssertionError(f"unexpected close control: {close.get_label()!r}")
            if "session keeps running" not in (close.get_tooltip_text() or ""):
                raise AssertionError("close tooltip does not explain session recovery semantics")
            if "Pinned" not in (label.get_tooltip_text() or ""):
                raise AssertionError("pinned state is not available through the tab tooltip")

            record = next(iter(window.tabs.values())).record
            renamed = app.store.update(record.id, name="api-renamed")
            window.refresh_tab_label(renamed)
            _drain_gtk(Gtk)
            renamed_label, renamed_close = _tab_parts(Gtk, window)
            if renamed_label.get_text() != "api-renamed":
                raise AssertionError("renamed tab did not refresh to the exact name")
            if renamed_close.get_label() != "×":
                raise AssertionError("rename changed the close control")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "initial_label": "client-api",
                        "renamed_label": "api-renamed",
                        "visible_decoration": False,
                        "close_control": "×",
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
