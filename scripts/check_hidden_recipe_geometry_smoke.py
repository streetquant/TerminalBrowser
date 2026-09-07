#!/usr/bin/env python3
"""Verify hidden GUI workspace payloads start at the canonical mapped VTE grid."""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

from smoke_support import require_private_validation_runtime


def _drain(Gtk, *, until=None, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return True
        time.sleep(0.005)
    return False


def _grid(payload: dict[str, object]) -> str:
    return f"{int(payload['columns'])}x{int(payload['rows'])}"


def main() -> int:
    runtime_base = require_private_validation_runtime()
    with tempfile.TemporaryDirectory(
        prefix="t1000-hidden-recipe-grid-"
    ) as tmp, tempfile.TemporaryDirectory(
        prefix="tdhr-",
        dir=runtime_base,
    ) as tmux_tmp_dir:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = Path(tmux_tmp_dir)
        home.mkdir()
        tmux_tmp.chmod(0o700)
        os.environ.update(HOME=str(home), TMUX_TMPDIR=str(tmux_tmp), NO_AT_BRIDGE="1")

        helper = root / "record_grid.py"
        helper.write_text(
            """from __future__ import annotations
import json, os, sys, time
from pathlib import Path
size = os.get_terminal_size()
Path(sys.argv[1]).write_text(json.dumps({"columns": size.columns, "rows": size.lines}) + "\\n", encoding="utf-8")
sys.stdout.write("\\x1b[?1049h\\x1b[2J\\x1b[HGRID %dx%d\\r\\n" % (size.columns, size.lines))
sys.stdout.flush()
time.sleep(30)
""",
            encoding="utf-8",
        )
        markers = {
            name: root / f"{name}.json"
            for name in ("mixed-visible", "mixed-hidden", "hidden-a", "hidden-b")
        }

        def command(name: str) -> str:
            return shlex.join([sys.executable, str(helper), str(markers[name])])

        recipe = root / "workspace.json"
        recipe.write_text(
            json.dumps(
                {
                    "name": "Hidden Recipe Geometry",
                    "windows": [
                        {
                            "title": "Mixed geometry",
                            "tabs": [
                                {
                                    "name": "mixed-visible",
                                    "tag": "terminal",
                                    "command": command("mixed-visible"),
                                    "cwd": str(root),
                                    "visible": True,
                                },
                                {
                                    "name": "mixed-hidden",
                                    "tag": "terminal",
                                    "command": command("mixed-hidden"),
                                    "cwd": str(root),
                                    "visible": False,
                                },
                            ],
                        },
                        {
                            "title": "All hidden geometry",
                            "tabs": [
                                {
                                    "name": "hidden-a",
                                    "tag": "terminal",
                                    "command": command("hidden-a"),
                                    "cwd": str(root),
                                    "visible": False,
                                },
                                {
                                    "name": "hidden-b",
                                    "tag": "terminal",
                                    "command": command("hidden-b"),
                                    "cwd": str(root),
                                    "visible": False,
                                },
                            ],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(db_path=root / "tabs.sqlite3", workspace_path=recipe)
        try:
            app.register(None)
            app.activate()
            if not _drain(Gtk, until=lambda: len(app.windows) == 2):
                raise AssertionError("workspace windows did not open")
            for window in app.windows:
                window.maximize()
            if not _drain(Gtk, until=lambda: all(path.exists() for path in markers.values())):
                raise AssertionError("one or more recipe payloads did not start")
            if not _drain(
                Gtk,
                until=lambda: all(
                    not window._recipe_geometry_source_id for window in app.windows
                ),
            ):
                raise AssertionError("recipe geometry barrier did not complete")

            windows = {window.window_title: window for window in app.windows}
            mixed = windows["Mixed geometry"]
            all_hidden = windows["All hidden geometry"]
            records = {record.name: record for record in app.store.list()}
            payloads = {name: json.loads(path.read_text()) for name, path in markers.items()}

            mixed_visible_grid = _grid(payloads["mixed-visible"])
            mixed_hidden_grid = _grid(payloads["mixed-hidden"])
            mixed_visible_record = records["mixed-visible"]
            mixed_hidden_record = records["mixed-hidden"]
            mixed_widget = mixed.tabs[mixed_visible_record.id]
            mixed_vte_grid = (
                f"{int(mixed_widget.widget.terminal.get_column_count())}x"
                f"{int(mixed_widget.widget.terminal.get_row_count())}"
            )
            mixed_hidden_tmux = app.backend._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    mixed_hidden_record.tmux_name,
                    "#{pane_width}x#{pane_height}",
                ]
            ).stdout.strip()
            if not (
                mixed_visible_grid
                == mixed_hidden_grid
                == mixed_hidden_tmux
                == mixed_vte_grid
            ):
                raise AssertionError(
                    "mixed visible/hidden grids diverged: "
                    f"visible={mixed_visible_grid}, hidden={mixed_hidden_grid}, "
                    f"hidden_tmux={mixed_hidden_tmux}, vte={mixed_vte_grid}"
                )
            if mixed_hidden_record.visible or mixed_hidden_record.id in mixed.tabs:
                raise AssertionError("mixed hidden row became a GUI tab during startup")

            hidden_initial = [_grid(payloads["hidden-a"]), _grid(payloads["hidden-b"])]
            canonical = f"{all_hidden._recipe_canonical_grid[0]}x{all_hidden._recipe_canonical_grid[1]}"
            if hidden_initial != [canonical, canonical]:
                raise AssertionError(
                    f"all-hidden first grids do not match canonical grid: {hidden_initial}, {canonical}"
                )
            if all_hidden.tabs or all_hidden.notebook.get_n_pages() != 0:
                raise AssertionError("geometry probe left a fake GUI tab behind")
            for name in ("hidden-a", "hidden-b"):
                record = records[name]
                current = app.store.get(record.id)
                if current is None or current.visible or current.status != "running":
                    raise AssertionError(f"hidden record state changed unexpectedly: {current}")
                tmux_grid = app.backend._run(
                    [
                        "display-message",
                        "-p",
                        "-t",
                        record.tmux_name,
                        "#{pane_width}x#{pane_height}",
                    ]
                ).stdout.strip()
                if tmux_grid != canonical:
                    raise AssertionError(f"{name} tmux grid {tmux_grid} != {canonical}")

            # Opening a background tab later must not resize the already-rendered
            # alternate-screen buffer to a different grid.
            opened = records["hidden-a"]
            if not all_hidden.open_user_session(opened):
                raise AssertionError("could not open the all-hidden background tab")
            if not _drain(
                Gtk,
                until=lambda: (
                    opened.id in all_hidden.tabs
                    and getattr(all_hidden.tabs[opened.id].widget.controller, "_ready", False)
                ),
            ):
                raise AssertionError("opened hidden tab did not attach")
            opened_widget = all_hidden.tabs[opened.id].widget
            opened_grid = (
                f"{int(opened_widget.terminal.get_column_count())}x"
                f"{int(opened_widget.terminal.get_row_count())}"
            )
            if opened_grid != canonical:
                raise AssertionError(
                    f"later VTE grid {opened_grid} resized first-frame grid {canonical}"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "mixed_canonical_grid": mixed_visible_grid,
                        "mixed_hidden_remained_hidden": True,
                        "all_hidden_canonical_grid": canonical,
                        "all_hidden_first_grids": hidden_initial,
                        "probe_rows": 0,
                        "probe_notebook_pages_after_launch": 0,
                        "later_open_grid": opened_grid,
                        "headless_api_unchanged": True,
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
