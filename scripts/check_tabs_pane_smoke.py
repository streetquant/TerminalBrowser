#!/usr/bin/env python3
"""Verify the terminal-first Tabs-pane lifecycle under a real GTK display."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def _drain_gtk(Gtk, *, until=None, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    raise RuntimeError("GTK state did not settle before timeout")


def _translated_origin(widget, destination) -> tuple[int, int]:
    result = widget.translate_coordinates(destination, 0, 0)
    if result is None:
        raise AssertionError("GTK could not translate the sidebar allocation")
    return int(result[-2]), int(result[-1])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-tabs-pane-smoke-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"
        config_dir = home / ".config" / "termdeck"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps({"appearance": {"ui_font_scale": 1.75}}),
            encoding="utf-8",
        )

        workspace = root / "workspace.json"
        workspace.write_text(
            json.dumps(
                {
                    "name": "Pane QA",
                    "windows": [
                        {
                            "title": "Backend",
                            "tabs": [
                                {
                                    "name": "backend",
                                    "tag": "terminal",
                                    "command": "{shell}",
                                    "cwd": str(root),
                                },
                                {
                                    "name": "frontend",
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

        # Import only after HOME/TMUX_TMPDIR are isolated because settings paths
        # are computed when the package is imported.
        from termdeck.gtk_app import Gdk, Gtk, TermDeckApplication

        app = TermDeckApplication(
            db_path=root / "tabs.sqlite3",
            workspace_path=workspace,
        )
        try:
            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: bool(app.windows))

            first = app.windows[0]
            # Keep the full window inside xvfb-run's default screen while still
            # leaving enough room for the maximum supported interface scale.
            first.move(0, 0)
            first.resize(1200, 760)
            _drain_gtk(Gtk)
            first.set_side_pane_position("left", persist=False)
            _drain_gtk(Gtk)
            if first.tabs_pane_visible or first.side.get_visible():
                raise AssertionError("Tabs pane must be hidden on a fresh/default launch")

            f9 = SimpleNamespace(keyval=Gdk.KEY_F9, state=0)
            if not first.on_key_press(first, f9):
                raise AssertionError("F9 did not handle the Tabs-pane toggle")
            _drain_gtk(Gtk)
            if not first.tabs_pane_visible or not first.side.get_visible():
                raise AssertionError("F9 did not show the Tabs pane")

            first.side.refresh()
            _drain_gtk(
                Gtk,
                until=lambda: (
                    first.side.get_allocated_width() > 100
                    and first.paned.get_position() >= first.side.get_preferred_width()[0]
                ),
            )
            side_x, _side_y = _translated_origin(first.side, first)
            side_children = [
                child
                for child in first.side.get_children()
                if child.get_visible()
            ]
            if not side_children:
                raise AssertionError("Tabs pane has no visible content children")
            # These widgets are direct GtkBox children of ``first.side``.  GTK
            # may legitimately return ``None`` when translating a no-window
            # child all the way to an ApplicationWindow during a resize frame;
            # its allocation remains authoritative in the parent coordinate
            # space and still catches left-edge clipping deterministically.
            content_x = side_x + min(
                int(child.get_allocation().x) for child in side_children
            )
            side_min_width, side_natural_width = first.side.get_preferred_width()
            side_allocated_width = first.side.get_allocated_width()
            side_split = first.paned.get_position()
            paned_width = first.paned.get_allocated_width()
            window_width = first.get_allocated_width()
            if side_x < 0 or content_x < side_x:
                raise AssertionError(
                    "left Tabs pane was clipped at the window edge: "
                    f"pane_x={side_x}, content_x={content_x}"
                )
            if side_split < side_min_width:
                raise AssertionError(
                    "left Tabs pane divider ignored its scaled minimum width: "
                    f"split={side_split}, minimum={side_min_width}"
                )
            if not first.side.session_box.get_activate_on_single_click():
                raise AssertionError("Tabs-pane rows are not configured for single-click activation")
            tab_rows = [
                row
                for row in first.side.session_box.get_children()
                if getattr(row, "_termdeck_session_record", None) is not None
            ]
            if len(tab_rows) < 2:
                raise AssertionError("Tabs pane did not expose another tab row for navigation")
            # Click the first, fully visible card. If it is already current,
            # put the second tab in front first so the click must navigate.
            target_row = tab_rows[0]
            target_id = target_row._termdeck_session_record.id
            if first.current_session_id() == target_id:
                other_record = tab_rows[1]._termdeck_session_record
                first.open_session(other_record)
                _drain_gtk(Gtk, until=lambda: first.current_session_id() == other_record.id)
            card_event = target_row.get_child()
            card_window = card_event.get_window()
            if card_window is None:
                raise AssertionError("target tab card was not realised")
            allocation = card_event.get_allocation()
            click_x = max(12, allocation.width // 2)
            click_y = 12
            _origin_ok, card_x, card_y = card_window.get_origin()
            subprocess.run(
                ["xdotool", "mousemove", "--sync", str(card_x + click_x), str(card_y + click_y)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _drain_gtk(Gtk)
            subprocess.run(
                ["xdotool", "mousedown", "1"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _drain_gtk(Gtk)
            subprocess.run(
                ["xdotool", "mouseup", "1"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _drain_gtk(Gtk)
            _drain_gtk(Gtk, until=lambda: first.current_session_id() == target_id)
            if first.current_session_id() != target_id:
                raise AssertionError("clicking a Tabs-pane row did not navigate to that tab")

            payload = json.loads(app.settings.path.read_text(encoding="utf-8"))
            if payload.get("appearance", {}).get("tabs_pane_visible") is not True:
                raise AssertionError("shown Tabs-pane state was not persisted")

            second = app.create_named_window("Research", open_initial_tab=False)
            _drain_gtk(
                Gtk,
                until=lambda: (
                    second.side.get_allocated_width() > 100
                    and second.paned.get_position()
                    <= second.paned.get_allocated_width()
                    - second.side.get_preferred_width()[0]
                ),
            )
            if not second.tabs_pane_visible or not second.side.get_visible():
                raise AssertionError("new windows did not inherit visible Tabs-pane state")
            second_side_x, _second_side_y = _translated_origin(second.side, second)
            second_side_right = second_side_x + second.side.get_allocated_width()
            # GTK's rounded CSS shadow/margin may extend a few pixels beyond
            # the content allocation; a whole clipped control is much larger.
            edge_tolerance = 8
            if (
                second_side_x < 0
                or second_side_right > second.get_allocated_width() + edge_tolerance
            ):
                raise AssertionError(
                    "right Tabs pane escaped the window allocation: "
                    f"left={second_side_x}, right={second_side_right}, "
                    f"window={second.get_allocated_width()}"
                )

            # The composer is captain-driven: an empty checkbox selection must
            # still submit to exactly one verified Codex tab in this window,
            # never broadcast to workers or cross into another window. Stub
            # only the provider probe/send edge; all GTK widgets, persistence,
            # envelope construction, and button/signal wiring remain real.
            second.new_shell_tab(root)
            _drain_gtk(Gtk, until=lambda: bool(second.tabs))
            foreign_id = next(iter(second.tabs))
            captain_id = first.current_session_id()
            if captain_id is None:
                raise AssertionError("Tabs pane smoke has no active captain candidate")
            local_ids = [session_id for session_id in first.tabs if session_id != captain_id]
            if not local_ids:
                raise AssertionError("Tabs pane smoke needs one same-window crew tab")
            first.side.selected_ids = {local_ids[0], foreign_id}
            original_probe = app.backend.is_codex_running
            original_send = app.backend.send_text
            captain_sends = []
            app.backend.is_codex_running = lambda record: record.id == captain_id
            app.backend.send_text = lambda record, text, enter=True: captain_sends.append(
                (record.id, text, bool(enter))
            )
            try:
                primary = next(
                    child
                    for child in first.side.get_children()
                    if isinstance(child, Gtk.Box)
                    and [item.get_label() for item in child.get_children() if isinstance(item, Gtk.Button)]
                    == ["Send", "Pause", "Resume", "Kill"]
                )
                buttons = {
                    item.get_label(): item
                    for item in primary.get_children()
                    if isinstance(item, Gtk.Button)
                }
                first.side.prompt.set_text("captain button smoke")
                buttons["Send"].clicked()
                _drain_gtk(Gtk, until=lambda: len(captain_sends) == 1)
                if captain_sends[0][0] != captain_id or not captain_sends[0][2]:
                    raise AssertionError(f"Send targeted the wrong tab/mode: {captain_sends[0][:1]}")
                envelope = captain_sends[0][1]
                if local_ids[0] not in envelope or foreign_id in envelope:
                    raise AssertionError("captain envelope escaped its logical window")
                if "captain button smoke" not in envelope:
                    raise AssertionError("captain envelope lost the user request")
                if first.side.prompt.get_text():
                    raise AssertionError("successful captain Send did not clear the composer")
                captain = app.store.captain_for_window(first.window_id)
                if captain is None or captain.id != captain_id:
                    raise AssertionError("captain assignment was not persisted")

                first.side.prompt.set_text("captain enter smoke")
                first.side.prompt.emit("activate")
                _drain_gtk(Gtk, until=lambda: len(captain_sends) == 2)
                if captain_sends[1][0] != captain_id or "captain enter smoke" not in captain_sends[1][1]:
                    raise AssertionError("Entry activation did not use the captain Send path")
            finally:
                app.backend.is_codex_running = original_probe
                app.backend.send_text = original_send

            if not first.on_key_press(first, f9):
                raise AssertionError("second F9 toggle was not handled")
            _drain_gtk(Gtk)
            if first.side.get_visible() or second.side.get_visible():
                raise AssertionError("Tabs-pane hiding did not synchronise across windows")

            payload = json.loads(app.settings.path.read_text(encoding="utf-8"))
            appearance = payload.get("appearance", {})
            if appearance.get("tabs_pane_visible") is not False:
                raise AssertionError("hidden Tabs-pane state was not persisted")
            if "focus_mode" in appearance or "focus" in appearance:
                raise AssertionError("legacy focus keys remained after canonical save")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "default_hidden": True,
                        "f9_toggle": True,
                        "persisted": True,
                        "new_window_inherits": True,
                        "row_click_navigation": True,
                        "captain_send_button": True,
                        "captain_entry_activate": True,
                        "captain_window_isolation": True,
                        "captain_does_not_broadcast": True,
                        "high_scale_edges_visible": True,
                        "sidebar_content_x": content_x,
                        "sidebar_min_width": side_min_width,
                        "sidebar_natural_width": side_natural_width,
                        "sidebar_allocated_width": side_allocated_width,
                        "sidebar_split": side_split,
                        "sidebar_x": side_x,
                        "right_sidebar_x": second_side_x,
                        "right_sidebar_right": second_side_right,
                        "paned_width": paned_width,
                        "window_width": window_width,
                        "all_windows_synchronise": True,
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
