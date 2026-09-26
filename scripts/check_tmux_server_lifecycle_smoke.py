#!/usr/bin/env python3
"""Prove the first private tmux server is independent of the caller lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path


def main() -> int:
    from termdeck.tmux_backend import TmuxBackend
    from termdeck.tmux_server_lifecycle import user_systemd_scope_available

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket-root-parent",
        type=Path,
        default=Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")),
        help="parent of the disposable socket directory (default: XDG_RUNTIME_DIR)",
    )
    args = parser.parse_args()

    if not user_systemd_scope_available():
        print(
            json.dumps(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "a reachable user systemd manager is unavailable",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    runtime_root = args.socket_root_parent.expanduser()
    if not runtime_root.is_dir() or not os.access(runtime_root, os.W_OK | os.X_OK):
        print(
            json.dumps(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": f"runtime directory is not writable: {runtime_root}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    with tempfile.TemporaryDirectory(
        prefix="t1v-",
        dir=runtime_root,
    ) as tmp:
        socket_root = Path(tmp)
        socket_root.chmod(0o700)
        os.environ["TMUX_TMPDIR"] = str(socket_root)
        os.environ.pop("TMUX", None)
        backend = TmuxBackend(
            executable="/usr/bin/tmux",
            command_timeout=5.0,
            query_timeout=1.0,
        )
        session_name = "termdeck_lifecycle_smoke"
        server_pid: int | None = None
        try:
            backend._run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "exec /usr/bin/sleep 600",
                ]
            )
            server_pid = int(
                backend._run(["display-message", "-p", "#{pid}"]).stdout.strip()
            )
            cgroup = Path(f"/proc/{server_pid}/cgroup").read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
            unit = Path(cgroup.split(":", 2)[-1]).name
            panes: list[str] = []
            for _ in range(100):
                panes = backend._run(
                    [
                        "list-panes",
                        "-a",
                        "-F",
                        "#{session_name}|#{pane_id}|#{pane_pid}|"
                        "#{pane_dead}|#{pane_current_command}",
                    ]
                ).stdout.splitlines()
                if len(panes) == 1 and panes[0].endswith("|0|sleep"):
                    break
                time.sleep(0.05)
            sessions = backend._run(
                [
                    "list-sessions",
                    "-F",
                    "#{session_name}|#{session_created}|#{session_windows}",
                ]
            ).stdout.splitlines()
            isolation = backend.ensure_server_lifecycle_isolated()
            if not (
                (
                    (
                        unit.startswith("t1000-tmux-server-")
                        and unit.endswith(".scope")
                    )
                    or (
                        unit.startswith("t1000-tmux-anchor-")
                        and unit.endswith(".service")
                    )
                )
                and len(sessions) == len(panes) == 1
                and sessions[0].startswith(f"{session_name}|")
                and panes[0].endswith("|0|sleep")
                and isolation.ok
                and not isolation.changed
                and isolation.server_pid == server_pid
                and isolation.unit == unit
            ):
                raise AssertionError(
                    f"tmux lifecycle scope was not established: "
                    f"unit={unit!r} sessions={sessions!r} panes={panes!r} "
                    f"isolation={isolation.as_dict()!r}"
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": False,
                        "server_pid": server_pid,
                        "server_unit": unit,
                        "session_count": len(sessions),
                        "pane_count": len(panes),
                        "postcheck_changed": isolation.changed,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            backend._run(["kill-server"], check=False, timeout=5.0)
            if server_pid is not None:
                for _ in range(100):
                    if not Path(f"/proc/{server_pid}").exists():
                        break
                    time.sleep(0.05)
                if Path(f"/proc/{server_pid}").exists():
                    raise RuntimeError(
                        "disposable tmux server survived lifecycle smoke cleanup"
                    )


if __name__ == "__main__":
    raise SystemExit(main())
