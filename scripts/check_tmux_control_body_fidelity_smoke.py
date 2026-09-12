#!/usr/bin/env python3
"""Prove tmux command replies preserve percent-prefixed pane text exactly.

Control mode frames command output with ``%begin`` and matching ``%end`` or
``%error`` records. tmux guarantees asynchronous notifications never occur
inside such a block. A parser that gives notification prefixes authority before
active reply body ownership can therefore drop legitimate capture-pane lines or
manufacture events. This smoke compares direct capture bytes with the same
command routed through ``ControlModeClient`` against a disposable real server.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from termdeck.pane_stream import control_attach_argv, spawn_control_transport
from termdeck.tmux_control import ClientState, ControlEvent, ControlModeClient


BODY_LINES = (
    "%output %999 literal-body",
    "%message literal-message",
    "%window-add @999",
    "%begin 999 999 0",
    "%end literal-suffix",
    "%end 7 999 0",
    "%error 7 4 denied",
    "%error literal-suffix",
    "Unicode • └ │ … ─ » — ’",
    "NORMAL",
)


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise RuntimeError("tmux control-mode state did not settle")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tdcb-") as tmp:
        root = Path(tmp)
        socket = root.name
        worker = root / "writer.py"
        worker.write_text(
            "import sys, time\n"
            f"lines = {BODY_LINES!r}\n"
            "sys.stdout.buffer.write(('\\n'.join(lines) + '\\n').encode('utf-8'))\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )

        def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                ["tmux", "-L", socket, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=check,
            )

        tmux("-f", "/dev/null", "new-session", "-d", "-s", "probe", sys.executable, str(worker))
        try:
            time.sleep(0.15)
            capture_args = ("capture-pane", "-epq", "-t", "probe:0.0", "-S", "-30")
            direct = tmux(*capture_args).stdout.splitlines()
            expected_prefix = tuple(line.encode("utf-8") for line in BODY_LINES)
            if tuple(direct[: len(expected_prefix)]) != expected_prefix:
                raise AssertionError(
                    "direct tmux capture did not preserve the fixture prefix: "
                    f"{direct[:len(expected_prefix)]!r}"
                )

            events: list[ControlEvent] = []
            attach = ["tmux", "-L", socket, "attach-session", "-t", "probe"]
            client = ControlModeClient(
                spawn_transport=lambda: spawn_control_transport(
                    control_attach_argv(attach), None, None
                ),
                on_event=events.append,
                reconnect_initial_seconds=0.01,
                reconnect_max_seconds=0.05,
            )
            client.start()
            try:
                _wait_until(lambda: client.state is ClientState.CONNECTED)
                # Let the attach greeting and session notification drain, then
                # isolate events emitted while capture-pane's reply is active.
                time.sleep(0.10)
                events.clear()
                command = " ".join(capture_args)
                result = client.send_command(command, timeout=5.0)
                if not result.ok:
                    raise AssertionError(f"control capture failed: {result.error}")
                if result.byte_lines != tuple(direct):
                    raise AssertionError(
                        "control command body diverged from direct tmux capture: "
                        f"direct={direct!r}, reply={result.byte_lines!r}"
                    )
                if events:
                    raise AssertionError(
                        "command body manufactured asynchronous events: "
                        f"{[(event.kind, event.raw) for event in events]!r}"
                    )
            finally:
                client.stop()

            direct_bytes = b"\n".join(direct)
            reply_bytes = b"\n".join(result.byte_lines)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "line_count": len(result.byte_lines),
                        "fixture_lines": len(BODY_LINES),
                        "direct_sha256": hashlib.sha256(direct_bytes).hexdigest(),
                        "reply_sha256": hashlib.sha256(reply_bytes).hexdigest(),
                        "percent_prefixed_body_preserved": True,
                        "fake_event_count": 0,
                        "unicode_preserved": expected_prefix[-2] in result.byte_lines,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            tmux("kill-server", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
