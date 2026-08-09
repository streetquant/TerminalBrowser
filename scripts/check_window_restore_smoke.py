#!/usr/bin/env python3
"""Verify window restore with exact Codex and an injected recovery backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _wait_for_attached_client(
    tmux_name: str,
    process: subprocess.Popen[str],
    log_path: Path,
    *,
    timeout: float = 8.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(
                f"secondary database GUI exited before attaching {tmux_name}: {detail}"
            )
        result = subprocess.run(
            ["tmux", "list-clients", "-F", "#{session_name}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if tmux_name in result.stdout.splitlines():
            return
        time.sleep(0.05)
    detail = log_path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(
        f"secondary database GUI did not attach {tmux_name}: {detail}"
    )


def main() -> int:
    # Keep TMUX_TMPDIR comfortably below the Unix-domain socket path limit,
    # including when CI places TMPDIR under a deep checkout path.
    with tempfile.TemporaryDirectory(prefix="td-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "m"
        fake_bin = root / "bin"
        recovery_cwd = root / "recovered-repo"
        recovery_marker = root / "recovered-command.txt"
        home.mkdir()
        tmux_tmp.mkdir(mode=0o700)
        fake_bin.mkdir()
        recovery_cwd.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/bin/sh\nprintf '%s\\n%s\\n' \"$*\" \"$PWD\" > \"$TD_RECOVERY_MARKER\"\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["TD_RECOVERY_MARKER"] = str(recovery_marker)

        from termdeck.checkpointing import CheckpointResult
        from termdeck.gtk_app import (
            Gtk,
            TermDeckApplication,
            _tabs_pane_records,
            application_id_for_database,
        )
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import TmuxBackend

        class SmokeCheckpointManager:
            """Simulate an explicitly injected validated-image recovery backend."""

            def __init__(self, directory: Path) -> None:
                self.directory = directory

            def restart_command(self, record) -> str:
                return record.command

            def session_dir(self, record) -> Path:
                path = self.directory / record.id
                path.mkdir(parents=True, exist_ok=True)
                return path

            @staticmethod
            def wrap_launch_command(_record, command: str) -> str:
                return command

            @staticmethod
            def checkpoint(record) -> CheckpointResult:
                return CheckpointResult(
                    record.id,
                    "checkpoint",
                    True,
                    True,
                    "simulated validated checkpoint",
                    1,
                )

            @staticmethod
            def clear(_record) -> None:
                return None

        app = TermDeckApplication(db_path=root / "sessions.sqlite3")
        app.backend.checkpoints = SmokeCheckpointManager(root / "checkpoints")
        secondary_db = root / "secondary.sqlite3"
        secondary_store = SessionStore(secondary_db)
        secondary_backend = TmuxBackend()
        secondary_process: subprocess.Popen[str] | None = None
        secondary_log = root / "secondary-gui.log"
        secondary_log_handle = None
        records = []
        secondary_records = []
        try:
            # Persist the exact state left by a host crash: the registry says
            # these tabs were running, but the tmux server and processes are
            # gone. Ten windows with three tabs each prove both that startup
            # restores the complete window catalog and that it does not defer
            # terminals at the former 24-terminal attachment limit.
            for window_index in range(1, 11):
                for tab_index in range(3):
                    record = app.store.create(
                        name=f"agent-{window_index}-{tab_index}",
                        tag="terminal",
                        command=os.environ.get("SHELL", "/bin/sh"),
                        cwd=str(root),
                        window_id=f"window-{window_index}",
                        window_title=f"Window {window_index}",
                        tab_index=tab_index,
                        visible=True,
                    )
                    records.append(
                        app.store.update(
                            record.id,
                            status="running",
                            last_pid=90_000 + len(records),
                        )
                    )

            # This tab originally launched a shell, then ran Codex inside it.
            # Its separately persisted recovery command must run before the
            # recreated tab returns to that shell.
            codex_session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
            exact_recovery_command = f"codex --yolo resume {codex_session_id}"
            records[0] = app.store.update(
                records[0].id,
                recovery_command=exact_recovery_command,
                recovery_cwd=str(recovery_cwd),
                last_opened_at=records[0].last_opened_at,
            )

            secondary = secondary_store.create(
                name="secondary-agent",
                tag="terminal",
                command=os.environ.get("SHELL", "/bin/sh"),
                cwd=str(root),
                window_id="secondary-window",
                window_title="Secondary registry",
                visible=True,
            )
            secondary_backend.create_or_resume(secondary)
            secondary_records.append(
                secondary_store.update(
                    secondary.id,
                    status="running",
                    last_pid=secondary_backend.pane_pid(secondary),
                )
            )

            secondary_application_id = application_id_for_database(secondary_db)
            if app.get_application_id() == secondary_application_id:
                raise AssertionError("separate database files shared one GApplication identity")
            app.register(None)
            if app.get_is_remote():
                raise AssertionError("primary database GUI unexpectedly registered as remote")
            app.activate()
            secondary_log_handle = secondary_log.open("w", encoding="utf-8")
            secondary_process = subprocess.Popen(
                [
                    os.environ.get("PYTHON", sys.executable),
                    "-m",
                    "termdeck.cli",
                    "gui",
                    "--db",
                    str(secondary_db),
                ],
                cwd=Path.cwd(),
                env=os.environ.copy(),
                text=True,
                stdout=secondary_log_handle,
                stderr=subprocess.STDOUT,
            )
            _wait_for_attached_client(
                secondary_records[0].tmux_name,
                secondary_process,
                secondary_log,
            )
            _drain_gtk(
                Gtk,
                until=lambda: (
                    len(app.windows) == 10
                    and sum(len(window.tabs) for window in app.windows) == 30
                    and recovery_marker.exists()
                ),
            )

            titles = {window.window_title for window in app.windows}
            expected = {f"Window {index}" for index in range(1, 11)}
            if titles != expected:
                raise AssertionError(
                    f"relaunch omitted remembered windows: expected={sorted(expected)}, "
                    f"actual={sorted(titles)}"
                )

            attached = sum(len(window.tabs) for window in app.windows)
            if attached != len(records):
                raise AssertionError(
                    "startup did not attach every remembered tab: "
                    f"attached={attached}, remembered={len(records)}"
                )
            refreshed = app.store.list()
            if len(refreshed) != len(records) or not all(record.visible for record in refreshed):
                raise AssertionError("crash recovery hid or dropped remembered tabs")
            restarted = [record for record in refreshed if app.backend.session_exists(record)]
            deferred = [record for record in refreshed if not app.backend.session_exists(record)]
            if len(restarted) != len(records) or deferred:
                raise AssertionError(
                    "crash recovery did not restart every remembered tab: "
                    f"restarted={len(restarted)}, deferred={len(deferred)}"
                )
            recovered = recovery_marker.read_text(encoding="utf-8").splitlines()
            if recovered != [f"--yolo resume {codex_session_id}", str(recovery_cwd)]:
                raise AssertionError(
                    "saved foreground Codex command or cwd was not restored: "
                    f"actual={recovered!r}"
                )
            for window in app.windows:
                owned = [record for record in records if record.window_id == window.window_id]
                pane_records = _tabs_pane_records(app.store, window.window_id)
                if len(owned) != 3 or {record.id for record in pane_records} != {
                    record.id for record in owned
                }:
                    raise AssertionError(
                        f"remembered tabs disappeared from {window.window_title}"
                    )

            # Simulate a partial single-instance activation: eight GTK windows
            # remain realized while two disappear. Reactivation must fill only
            # the missing catalog IDs instead of treating a non-empty
            # ``app.windows`` list as proof that restore is complete.
            removed_ids = {window.window_id for window in app.windows[-2:]}
            for window in app.windows[-2:]:
                window.destroy()
            _drain_gtk(Gtk, until=lambda: len(app.windows) == 8)
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: (
                    len(app.windows) == 10
                    and sum(len(window.tabs) for window in app.windows) == 30
                ),
            )
            restored_ids = {window.window_id for window in app.windows}
            if restored_ids != {f"window-{index}" for index in range(1, 11)}:
                raise AssertionError(
                    "reactivation did not reconstruct the complete unique window set: "
                    f"actual={sorted(restored_ids)}"
                )
            if not removed_ids.issubset(restored_ids):
                raise AssertionError("reactivation omitted one of the two removed windows")

            print(
                json.dumps(
                    {
                        "all_remembered_windows_restored": sorted(titles),
                        "attached_at_startup": attached,
                        "all_tabs_preserved": len(refreshed),
                        "foreground_command_relaunched": exact_recovery_command,
                        "foreground_command_cwd": str(recovery_cwd),
                        "ok": True,
                        "partially_missing_windows_reconstructed": sorted(removed_ids),
                        "restarted_after_crash": len(restarted),
                        "separate_database_instances": [
                            app.get_application_id(),
                            secondary_application_id,
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            for record in records:
                try:
                    if app.backend.session_exists(record):
                        app.backend.terminate(record)
                except Exception:
                    pass
            if secondary_process is not None and secondary_process.poll() is None:
                secondary_process.terminate()
                try:
                    secondary_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    secondary_process.kill()
                    secondary_process.wait(timeout=5)
            if secondary_log_handle is not None:
                secondary_log_handle.close()
            for record in secondary_records:
                try:
                    if secondary_backend.session_exists(record):
                        secondary_backend.terminate(record)
                except Exception:
                    pass
            for window in list(Gtk.Window.list_toplevels()):
                try:
                    window.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()
            secondary_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
