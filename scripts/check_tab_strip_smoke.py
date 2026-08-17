#!/usr/bin/env python3
"""Verify that notebook tabs show only their exact names and close controls."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
from pathlib import Path

from smoke_support import stream_ready


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

    labels = []
    buttons = []

    def collect(widget) -> None:
        if isinstance(widget, Gtk.Button):
            # A Gtk.Button may implement its caption with an internal label;
            # that is the close control, not a second tab-name label.
            buttons.append(widget)
            return
        if isinstance(widget, Gtk.Label):
            labels.append(widget)
            return
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                collect(child)

    collect(tab_label)
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
        live_workdir = root / "live-cwd"
        renamed_workdir = root / "renamed-cwd"
        reopened_workdir = root / "reopened-cwd"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        workdir.mkdir()
        live_workdir.mkdir()
        renamed_workdir.mkdir()
        reopened_workdir.mkdir()
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
            if label.get_width_chars() != len("client-api"):
                raise AssertionError("tab label does not reserve readable natural width")
            if label.get_max_width_chars() != 20:
                raise AssertionError("tab label width cap changed")
            if close.get_label() != "×":
                raise AssertionError(f"unexpected close control: {close.get_label()!r}")
            if "session keeps running" not in (close.get_tooltip_text() or ""):
                raise AssertionError("close tooltip does not explain session recovery semantics")
            if "Pinned" not in (label.get_tooltip_text() or ""):
                raise AssertionError("pinned state is not available through the tab tooltip")

            record = next(iter(window.tabs.values())).record
            app.backend.send_text(record, f"cd {shlex.quote(str(live_workdir))}")
            _drain_gtk(
                Gtk,
                until=lambda: app.backend.current_directory(record) == str(live_workdir.resolve()),
            )
            app.backend.invalidate_cache()
            if window.refresh_tab_directories() != 1:
                raise AssertionError("live cwd refresh did not update the directory-derived tab")
            tracked_label, _tracked_close = _tab_parts(Gtk, window)
            if tracked_label.get_text() != "live-cwd":
                raise AssertionError(
                    f"tab name did not follow live cwd: {tracked_label.get_text()!r}"
                )

            record = app.store.get(record.id)
            if record is None:
                raise AssertionError("tracked tab disappeared from the registry")
            renamed = app.store.update(record.id, name="api-renamed")
            window.refresh_tab_label(renamed)
            app.backend.send_text(renamed, f"cd {shlex.quote(str(renamed_workdir))}")
            _drain_gtk(
                Gtk,
                until=lambda: app.backend.current_directory(renamed) == str(renamed_workdir.resolve()),
            )
            app.backend.invalidate_cache()
            if window.refresh_tab_directories() != 1:
                raise AssertionError("custom-named tab cwd did not refresh")
            _drain_gtk(Gtk)
            renamed_label, renamed_close = _tab_parts(Gtk, window)
            if renamed_label.get_text() != "api-renamed":
                raise AssertionError("renamed tab did not refresh to the exact name")
            if renamed_close.get_label() != "×":
                raise AssertionError("rename changed the close control")

            # Reproduce a rapid ``cd`` followed immediately by closing the tab.
            # The close path must snapshot tmux's live cwd before detaching so
            # Ctrl+Shift+T can reattach the same session in the right directory.
            app.backend.send_text(renamed, f"cd {shlex.quote(str(reopened_workdir))}")
            _drain_gtk(
                Gtk,
                until=lambda: app.backend.current_directory(renamed)
                == str(reopened_workdir.resolve()),
            )
            if not window.hide_tab(renamed.id, include_pinned=True):
                raise AssertionError("close path did not hide the live tab")
            closed = app.store.get(renamed.id)
            if closed is None or closed.visible:
                raise AssertionError("closed tab was not retained as hidden")
            if closed.cwd != str(reopened_workdir.resolve()):
                raise AssertionError(
                    f"close snapshot stored {closed.cwd!r}, expected {str(reopened_workdir.resolve())!r}"
                )
            if window.reopen_last_closed_tab() != 1:
                raise AssertionError("reopen-last-closed did not restore the hidden tab")
            _drain_gtk(
                Gtk,
                until=lambda: renamed.id in window.tabs
                and stream_ready(window.tabs[renamed.id].widget),
            )
            reopened = app.store.get(renamed.id)
            if reopened is None or not reopened.visible:
                raise AssertionError("reopened tab was not marked visible")
            if app.backend.current_directory(reopened) != str(reopened_workdir.resolve()):
                raise AssertionError("reopened tab did not retain its live directory")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "initial_label": "client-api",
                        "tracked_cwd_label": "live-cwd",
                        "renamed_label": "api-renamed",
                        "reopened_directory": str(reopened_workdir.resolve()),
                        "reopened_same_session": True,
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
