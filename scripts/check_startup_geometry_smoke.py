#!/usr/bin/env python3
"""Verify visible and hidden pane streams use the final VTE geometry.

This catches the dotted tmux padding frame that appears when the control-mode
stream is initialized at 80x24 and the GTK window is maximized immediately
afterwards, including on a notebook page that GTK does not allocate while it is
hidden. A control-mode tmux client deliberately has no PTY and therefore no
``client_width``/``client_height`` oracle; the authoritative contract is the
requested controller grid, tmux window/pane grids, and VTE grid.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def _drain_gtk(Gtk) -> None:
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def _parse_size(value: str) -> tuple[int, int]:
    width, height = value.strip().split("x", 1)
    return int(width), int(height)


def _controller_grid(tab) -> tuple[int, int] | None:
    controller = getattr(tab.widget, "controller", None)
    if controller is None:
        return None
    lock = getattr(controller, "_lock", None)
    if lock is None:
        return None
    with lock:
        if not getattr(controller, "_ready", False):
            return None
        columns, rows = getattr(controller, "_size", (0, 0))
    return int(columns), int(rows)


def _wait_for_matching_grid(
    Gtk,
    window,
    record,
    tab,
    *,
    expected: tuple[int, int] | None = None,
    different_from: tuple[int, int] | None = None,
    timeout: float = 2.0,
):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        _drain_gtk(Gtk)
        controller = _controller_grid(tab)
        if controller is not None:
            vte = (
                int(tab.widget.terminal.get_column_count()),
                int(tab.widget.terminal.get_row_count()),
            )
            tmux_window = _parse_size(
                subprocess.check_output(
                    [
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        record.tmux_name,
                        "#{window_width}x#{window_height}",
                    ],
                    text=True,
                ).strip()
            )
            tmux_pane = _parse_size(
                subprocess.check_output(
                    [
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        record.tmux_name,
                        "#{pane_width}x#{pane_height}",
                    ],
                    text=True,
                ).strip()
            )
            last = (controller, tmux_window, tmux_pane, vte)
            matches_expected = expected is None or vte == expected
            changed = different_from is None or vte != different_from
            if (
                controller == tmux_window == tmux_pane == vte
                and matches_expected
                and changed
            ):
                return last
        time.sleep(0.01)
    raise AssertionError(f"terminal grid did not consume its allocation: {last}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-startup-geometry-") as tmp:
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
                    "name": "Geometry QA",
                    "windows": [
                        {
                            "title": "Geometry",
                            "tabs": [
                                {
                                    "name": "shell-hidden",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                },
                                {
                                    "name": "shell-current",
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

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not app.windows:
                _drain_gtk(Gtk)
                time.sleep(0.005)
            if not app.windows:
                raise AssertionError("TermDeck window did not open")

            window = app.windows[0]
            window.maximize()
            records = {record.name: record for record in app.store.list()}
            record = records["shell-current"]
            hidden_record = records["shell-hidden"]
            tab = window.tabs[record.id]
            hidden_tab = window.tabs[hidden_record.id]

            started = time.monotonic()
            first_controller_size: tuple[int, int] | None = None
            first_vte_size: tuple[int, int] | None = None
            first_window_size: tuple[int, int] | None = None
            first_pane_size: tuple[int, int] | None = None
            first_attach_ms: float | None = None
            deadline = started + 3.0
            while time.monotonic() < deadline:
                _drain_gtk(Gtk)
                controller_grid = _controller_grid(tab)
                if controller_grid is not None:
                    first_attach_ms = (time.monotonic() - started) * 1000
                    first_controller_size = controller_grid
                    first_vte_size = (
                        int(tab.widget.terminal.get_column_count()),
                        int(tab.widget.terminal.get_row_count()),
                    )
                    tmux_window = subprocess.check_output(
                        [
                            "tmux",
                            "display-message",
                            "-p",
                            "-t",
                            record.tmux_name,
                            "#{window_width}x#{window_height}",
                        ],
                        text=True,
                    ).strip()
                    first_window_size = _parse_size(tmux_window)
                    tmux_pane = subprocess.check_output(
                        [
                            "tmux",
                            "display-message",
                            "-p",
                            "-t",
                            record.tmux_name,
                            "#{pane_width}x#{pane_height}",
                        ],
                        text=True,
                    ).strip()
                    first_pane_size = _parse_size(tmux_pane)
                    break
                time.sleep(0.005)

            if (
                first_controller_size is None
                or first_vte_size is None
                or first_window_size is None
                or first_pane_size is None
            ):
                raise AssertionError("pane stream did not become ready within 3 seconds")
            if first_controller_size != first_vte_size:
                raise AssertionError(
                    "first stream-controller size "
                    f"{first_controller_size} did not match VTE {first_vte_size}"
                )
            if first_window_size != first_vte_size:
                raise AssertionError(
                    f"first tmux window size {first_window_size} did not match VTE {first_vte_size}"
                )
            if first_pane_size != first_vte_size:
                raise AssertionError(
                    f"first tmux pane size {first_pane_size} did not match VTE {first_vte_size}"
                )
            if first_controller_size == (80, 24) and first_vte_size != (80, 24):
                raise AssertionError(
                    "pane stream initialized at default 80x24 before final VTE allocation"
                )

            # The first-created page is hidden by the second page before the
            # maximize settles. It must already match the mapped page's grid;
            # selecting it later must not be the event that repairs tmux.
            current_page = window.notebook.page_num(tab.widget)
            if window.notebook.get_current_page() != current_page:
                raise AssertionError("expected shell-current to remain the selected page")
            hidden_before_select_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                expected=first_vte_size,
            )
            if window.notebook.get_current_page() != current_page:
                raise AssertionError("hidden geometry check selected the hidden page")

            window.set_tabs_pane_visible(True, persist=False, notify=False)
            pane_visible_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                different_from=first_vte_size,
            )
            pane_visible_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                expected=pane_visible_sizes[3],
            )
            window.set_tabs_pane_visible(False, persist=False, notify=False)
            pane_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                expected=first_vte_size,
            )
            pane_hidden_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                expected=pane_hidden_sizes[3],
            )

            # The first tab was hidden while the window maximized. Switching
            # through Gtk.Notebook (the real tab-click path) must clear stale
            # tmux mode and synchronize that newly mapped page's VTE grid.
            hidden_page = window.notebook.page_num(hidden_tab.widget)
            window.notebook.set_current_page(hidden_page)
            switched_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                expected=first_vte_size,
            )
            current_page = window.notebook.page_num(tab.widget)
            window.notebook.set_current_page(current_page)
            switched_current_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                expected=first_vte_size,
            )

            # Font zoom changes character metrics for both visible and hidden
            # notebook pages. Every attach client must converge to its own VTE
            # grid without relying on a later tab switch to repair it.
            window.zoom_font(-2)
            zoomed_current_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                different_from=first_vte_size,
            )
            zoomed_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                different_from=first_vte_size,
            )
            window.zoom_font_reset()
            reset_current_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                expected=first_vte_size,
            )
            reset_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                hidden_record,
                hidden_tab,
                expected=first_vte_size,
            )

            _drain_gtk(Gtk)
            root_widget = window.get_child()
            root_alloc = root_widget.get_allocation()
            paned_alloc = window.paned.get_allocation()
            if (root_alloc.width, root_alloc.height) != (paned_alloc.width, paned_alloc.height):
                raise AssertionError(
                    "main paned surface did not cover the full client allocation: "
                    f"root={root_alloc.width}x{root_alloc.height}, "
                    f"paned={paned_alloc.width}x{paned_alloc.height}"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "first_attach_ms": round(first_attach_ms or 0.0, 2),
                        "vte_grid": f"{first_vte_size[0]}x{first_vte_size[1]}",
                        "stream_controller_grid": (
                            f"{first_controller_size[0]}x{first_controller_size[1]}"
                        ),
                        "tmux_window_grid": f"{first_window_size[0]}x{first_window_size[1]}",
                        "tmux_pane_grid": f"{first_pane_size[0]}x{first_pane_size[1]}",
                        "client_surface_filled": True,
                        "hidden_before_select_grid": f"{hidden_before_select_sizes[3][0]}x{hidden_before_select_sizes[3][1]}",
                        "pane_visible_grid": f"{pane_visible_sizes[3][0]}x{pane_visible_sizes[3][1]}",
                        "pane_visible_hidden_grid": f"{pane_visible_hidden_sizes[3][0]}x{pane_visible_hidden_sizes[3][1]}",
                        "pane_hidden_grid": f"{pane_hidden_sizes[3][0]}x{pane_hidden_sizes[3][1]}",
                        "pane_hidden_hidden_grid": f"{pane_hidden_hidden_sizes[3][0]}x{pane_hidden_hidden_sizes[3][1]}",
                        "switched_hidden_grid": f"{switched_hidden_sizes[3][0]}x{switched_hidden_sizes[3][1]}",
                        "switched_current_grid": f"{switched_current_sizes[3][0]}x{switched_current_sizes[3][1]}",
                        "zoomed_current_grid": f"{zoomed_current_sizes[3][0]}x{zoomed_current_sizes[3][1]}",
                        "zoomed_hidden_grid": f"{zoomed_hidden_sizes[3][0]}x{zoomed_hidden_sizes[3][1]}",
                        "reset_current_grid": f"{reset_current_sizes[3][0]}x{reset_current_sizes[3][1]}",
                        "reset_hidden_grid": f"{reset_hidden_sizes[3][0]}x{reset_hidden_sizes[3][1]}",
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
