#!/usr/bin/env python3
"""Verify first tmux attachment uses the final VTE geometry.

This catches the dotted tmux padding frame that appears when a client attaches at
80x24 and the GTK window is maximized immediately afterwards.
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
        clients = subprocess.run(
            ["tmux", "list-clients", "-F", "#{client_width}x#{client_height}"],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip().splitlines()
        if clients:
            client = _parse_size(clients[0])
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
            last = (client, tmux_window, vte)
            matches_expected = expected is None or vte == expected
            changed = different_from is None or vte != different_from
            if client == tmux_window == vte and matches_expected and changed:
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
                                    "name": "shell",
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
            record = app.store.list()[0]
            tab = window.tabs[record.id]

            started = time.monotonic()
            first_client_size: tuple[int, int] | None = None
            first_vte_size: tuple[int, int] | None = None
            first_window_size: tuple[int, int] | None = None
            first_attach_ms: float | None = None
            deadline = started + 1.5
            while time.monotonic() < deadline:
                _drain_gtk(Gtk)
                clients = subprocess.run(
                    ["tmux", "list-clients", "-F", "#{client_width}x#{client_height}"],
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout.strip().splitlines()
                if clients:
                    first_attach_ms = (time.monotonic() - started) * 1000
                    first_client_size = _parse_size(clients[0])
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
                    break
                time.sleep(0.005)

            if first_client_size is None or first_vte_size is None or first_window_size is None:
                raise AssertionError("tmux client did not attach within 1.5 seconds")
            if first_client_size != first_vte_size:
                raise AssertionError(
                    f"first tmux client size {first_client_size} did not match VTE {first_vte_size}"
                )
            if first_window_size != first_vte_size:
                raise AssertionError(
                    f"first tmux window size {first_window_size} did not match VTE {first_vte_size}"
                )
            if first_client_size == (80, 24) and first_vte_size != (80, 24):
                raise AssertionError("tmux attached at default 80x24 before final VTE allocation")

            window.set_tabs_pane_visible(True, persist=False, notify=False)
            pane_visible_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
                different_from=first_vte_size,
            )
            window.set_tabs_pane_visible(False, persist=False, notify=False)
            pane_hidden_sizes = _wait_for_matching_grid(
                Gtk,
                window,
                record,
                tab,
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
                        "tmux_client_grid": f"{first_client_size[0]}x{first_client_size[1]}",
                        "tmux_window_grid": f"{first_window_size[0]}x{first_window_size[1]}",
                        "client_surface_filled": True,
                        "pane_visible_grid": f"{pane_visible_sizes[2][0]}x{pane_visible_sizes[2][1]}",
                        "pane_hidden_grid": f"{pane_hidden_sizes[2][0]}x{pane_hidden_sizes[2][1]}",
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
