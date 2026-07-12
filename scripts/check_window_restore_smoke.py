#!/usr/bin/env python3
"""Verify relaunch seeds remembered windows across the global VTE budget."""

from __future__ import annotations

import json
import os
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-window-restore-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(db_path=root / "sessions.sqlite3")
        # Do not forward activation to a developer's already-running TermDeck.
        app.set_application_id(f"dev.termdeck.WindowRestoreSmoke.p{os.getpid()}")
        records = []
        try:
            for index in range(1, 6):
                record = app.store.create(
                    name=f"agent-{index}",
                    tag="terminal",
                    command=os.environ.get("SHELL", "/bin/sh"),
                    cwd=str(root),
                    window_id=f"window-{index}",
                    window_title=f"Window {index}",
                    tab_index=0,
                    visible=True,
                )
                app.backend.create_or_resume(record)
                record = app.store.update(
                    record.id,
                    status="running",
                    last_pid=app.backend.pane_pid(record),
                )
                records.append(record)

            app.register(None)
            app.activate()
            _drain_gtk(Gtk, until=lambda: len(app.windows) == 5)

            titles = {window.window_title for window in app.windows}
            expected = {f"Window {index}" for index in range(1, 6)}
            if titles != expected:
                raise AssertionError(
                    f"relaunch omitted remembered windows: expected={sorted(expected)}, "
                    f"actual={sorted(titles)}"
                )
            if not all(app.backend.session_exists(record) for record in records):
                raise AssertionError("relaunch lost a remembered tmux session")

            attached = sum(len(window.tabs) for window in app.windows)
            if attached > 24:
                raise AssertionError(f"startup exceeded terminal attachment budget: {attached}")
            if attached != len(records):
                raise AssertionError(
                    "startup did not seed every remembered window while global capacity "
                    f"remained: attached={attached}, records={len(records)}"
                )
            for window in app.windows:
                owned = [record for record in records if record.window_id == window.window_id]
                if len(owned) != 1 or set(window.tabs) != {owned[0].id}:
                    raise AssertionError(
                        f"remembered window was not seeded with its own tab: {window.window_title}"
                    )

            print(
                json.dumps(
                    {
                        "all_remembered_windows_restored": sorted(titles),
                        "attached_at_startup": attached,
                        "all_windows_seeded": True,
                        "ok": True,
                        "sessions_survived": len(records),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for record in records:
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
