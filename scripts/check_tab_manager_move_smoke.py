#!/usr/bin/env python3
"""Verify right-click relocation into a new window under real GTK/tmux."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def _drain_gtk(Gtk, *, until=None, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    raise RuntimeError("GTK state did not settle before timeout")


def _row_path(model, session_id: str):
    iter_ = model.get_iter_first()
    while iter_ is not None:
        if model.get_value(iter_, 0) == session_id:
            return model.get_path(iter_)
        iter_ = model.iter_next(iter_)
    raise AssertionError(f"Tab Manager did not contain {session_id}")


def _right_click_tree_row(Gtk, tree, path) -> None:
    column = tree.get_column(2)
    area = tree.get_cell_area(path, column)
    bin_window = tree.get_bin_window()
    if bin_window is None:
        raise AssertionError("Tab Manager tree was not realised")
    origin = bin_window.get_origin()
    origin_x, origin_y = int(origin[-2]), int(origin[-1])
    x = origin_x + int(area.x) + max(8, min(int(area.width) // 2, 80))
    y = origin_y + int(area.y) + max(4, int(area.height) // 2)
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(x), str(y)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _drain_gtk(Gtk)
    subprocess.run(
        ["xdotool", "mousedown", "3"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _drain_gtk(Gtk)
    subprocess.run(
        ["xdotool", "mouseup", "3"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-manager-move-smoke-") as tmp:
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
                    "name": "Manager move QA",
                    "windows": [
                        {
                            "title": "Source",
                            "tabs": [
                                {
                                    "name": "move-me",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                }
                            ],
                        },
                        {
                            "title": "Destination",
                            "tabs": [
                                {
                                    "name": "keep-me",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                }
                            ],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Import only after HOME/TMUX_TMPDIR are isolated because package-level
        # settings paths are resolved during import.
        from gi.repository import GLib

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "sessions.sqlite3",
            workspace_path=workspace,
        )
        # Do not forward activation to a developer's already-running TermDeck.
        app.set_application_id(f"dev.termdeck.ManagerMoveSmoke.p{os.getpid()}")
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: len(app.windows) == 2)
            source = next(window for window in app.windows if window.window_title == "Source")
            existing_destination = next(
                window for window in app.windows if window.window_title == "Destination"
            )
            source.move(0, 0)
            existing_destination.move(700, 0)
            source.resize(680, 560)
            existing_destination.resize(680, 560)
            _drain_gtk(Gtk)

            record = next(row for row in app.store.list(window_id=source.window_id) if row.name == "move-me")
            moved_tab = source.tabs[record.id]
            moved_widget = moved_tab.widget
            _drain_gtk(
                Gtk,
                until=lambda: moved_widget.process is not None and moved_widget.process.poll() is None,
            )
            process = moved_widget.process
            if process is None:
                raise AssertionError("source tab did not start its tmux client")
            client_pid = process.pid
            pane_pid = app.backend.pane_pid(record)

            source.open_master_window()
            manager = source.master_window
            if manager is None:
                raise AssertionError("Tab Manager did not open")
            manager.move(60, 60)
            manager.resize(1180, 620)
            manager.refresh()
            manager.show_all()
            manager.present()
            _drain_gtk(Gtk, until=lambda: manager.tree.get_bin_window() is not None)
            path = _row_path(manager.store, record.id)

            _right_click_tree_row(Gtk, manager.tree, path)
            _drain_gtk(Gtk, until=lambda: getattr(manager, "_row_context_menu", None) is not None)
            if manager.selected_ids() != [record.id]:
                raise AssertionError(
                    "right-click did not target exactly the clicked manager row: "
                    f"selected={manager.selected_ids()}"
                )

            menu = manager._row_context_menu
            move_items = [item for item in menu.get_children() if item.get_label() == "Move to window"]
            if len(move_items) != 1:
                raise AssertionError("right-click menu did not expose Move to window")
            submenu = move_items[0].get_submenu()
            if submenu is None:
                raise AssertionError("Move to window did not expose a destination submenu")
            destinations = {
                item.get_label(): item
                for item in submenu.get_children()
                if item.get_sensitive()
            }
            if set(destinations) != {"Destination", "New window…"}:
                raise AssertionError(
                    "move destinations did not exclude the source window: "
                    f"destinations={sorted(destinations)}"
                )

            def accept_new_window_dialog() -> bool:
                for toplevel in Gtk.Window.list_toplevels():
                    if toplevel.get_title() != "New TermDeck window":
                        continue
                    toplevel.entry.set_text("Created destination")
                    toplevel.response(Gtk.ResponseType.OK)
                    return False
                return True

            GLib.timeout_add(20, accept_new_window_dialog)
            destinations["New window…"].activate()
            destination = next(
                window
                for window in app.windows
                if window.window_title == "Created destination"
            )
            _drain_gtk(
                Gtk,
                until=lambda: (
                    record.id not in source.tabs
                    and destination.tabs.get(record.id) is moved_tab
                    and destination.current_session_id() == record.id
                ),
            )
            if set(destination.tabs) != {record.id}:
                raise AssertionError(
                    "new destination created an extra shell instead of receiving only "
                    f"the moved tab: tabs={sorted(destination.tabs)}"
                )
            if len(existing_destination.tabs) != 1:
                raise AssertionError("moving to a new window changed the existing destination")

            refreshed = app.store.get(record.id)
            if refreshed is None:
                raise AssertionError("moved tab disappeared from the registry")
            if (
                refreshed.window_id != destination.window_id
                or refreshed.window_title != destination.window_title
            ):
                raise AssertionError(
                    "persisted ownership did not follow the GTK move: "
                    f"window_id={refreshed.window_id}, title={refreshed.window_title}"
                )
            if destination.tabs[record.id].widget is not moved_widget:
                raise AssertionError("move replaced the live VTE widget")
            if moved_widget.process is not process or process.pid != client_pid or process.poll() is not None:
                raise AssertionError("move restarted or stopped the existing tmux client")
            if moved_widget._detached:
                raise AssertionError("move detached the existing terminal widget")
            if not app.backend.session_exists(refreshed):
                raise AssertionError("tmux session disappeared during relocation")
            if app.backend.pane_pid(refreshed) != pane_pid:
                raise AssertionError("tmux pane process changed during relocation")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "right_click_row": True,
                        "source_excluded": True,
                        "same_vte_widget": True,
                        "same_tmux_client_pid": client_pid,
                        "same_tmux_pane_pid": pane_pid,
                        "destination_focused": True,
                        "new_window_destination": True,
                        "no_extra_shell": True,
                        "persisted_window": refreshed.window_title,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for row in app.store.list():
                try:
                    app.backend.terminate(row)
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
