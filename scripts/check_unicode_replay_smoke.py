#!/usr/bin/env python3
"""Prove tmux capture replay preserves Codex-style UTF-8 through real VTE.

Tmux control-mode command replies are decoded with Latin-1 as a reversible byte
carrier.  The initial history/screen replay must restore those bytes exactly;
encoding the carrier as text produces visible mojibake such as ``â¢`` in place
of Codex's ``•`` while later live ``%output`` remains correct.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time


def _drain_gtk(Gtk, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def _wait_until(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise RuntimeError("unicode replay did not become ready")


def main() -> int:
    from termdeck.ansi_contrast import SgrContrastFilter
    from termdeck.gtk_app import Gtk, Vte, apply_theme
    from termdeck.models import TerminalTheme
    from termdeck.pane_stream import PaneStreamController

    ready, _argv = Gtk.init_check([])
    if not ready:
        raise RuntimeError("GTK display is unavailable; run this smoke under xvfb-run")

    with tempfile.TemporaryDirectory(prefix="termdeck-unicode-replay-") as tmp:
        root = Path(tmp)
        socket = f"unicode-replay-{os.getpid()}"
        session = "unicode-replay"
        worker = root / "unicode_worker.py"
        worker.write_text(
            """import time
print("• HISTORY_MARKER └", flush=True)
for index in range(24):
    print(f"filler-{index:02d}", flush=True)
print("\\x1b[38;2;16;16;16m│ VISIBLE_MARKER … ─\\x1b[0m", flush=True)
time.sleep(30)
""",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["LC_ALL"] = "C.UTF-8"
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "72",
                "-y",
                "8",
                "-s",
                session,
                shlex.join([sys.executable, str(worker)]),
            ],
            check=True,
            env=environment,
            timeout=10,
        )

        controller: PaneStreamController | None = None
        window = Gtk.Window()
        try:
            time.sleep(0.2)
            direct = subprocess.check_output(
                [
                    "tmux",
                    "-L",
                    socket,
                    "capture-pane",
                    "-epq",
                    "-t",
                    session,
                    "-S",
                    "-100",
                ],
                env=environment,
                timeout=10,
            )
            history = "• HISTORY_MARKER └".encode("utf-8")
            visible = "│ VISIBLE_MARKER … ─".encode("utf-8")
            for marker in (history, visible):
                if marker not in direct:
                    raise AssertionError(f"authoritative tmux capture lost {marker!r}")

            chunks: list[bytes] = []
            lock = threading.Lock()

            def on_output(payload: bytes) -> None:
                with lock:
                    chunks.append(payload)

            def replay_bytes() -> bytes:
                with lock:
                    return b"".join(chunks)

            controller = PaneStreamController(
                attach_argv=[
                    "tmux",
                    "-L",
                    socket,
                    "attach-session",
                    "-t",
                    session,
                ],
                on_output=on_output,
                history_lines=100,
                command_timeout=2.0,
                environment=environment,
            )
            controller.start(columns=72, rows=8)
            _wait_until(lambda: bool(getattr(controller, "_ready", False)))
            replay = replay_bytes()
            for marker in (history, visible):
                if marker not in replay:
                    raise AssertionError(f"pane replay corrupted {marker!r}: {replay!r}")
                mojibake = marker.decode("latin-1").encode("utf-8")
                if mojibake in replay:
                    raise AssertionError(f"pane replay double-encoded {marker!r}")

            theme = TerminalTheme(font="Monospace 18")
            terminal = Vte.Terminal()
            terminal.set_size(72, 8)
            terminal.set_scrollback_lines(1000)
            apply_theme(terminal, theme, font_override="Monospace 18")
            window.add(terminal)
            window.resize(1200, 420)
            window.show_all()

            contrast = SgrContrastFilter(theme)
            filtered = bytearray()
            widths = (1, 2, 3, 5, 8, 13)
            offset = 0
            step = 0
            while offset < len(replay):
                width = widths[step % len(widths)]
                filtered += contrast.process(replay[offset : offset + width])
                offset += width
                step += 1
            terminal.feed(bytes(filtered))
            _drain_gtk(Gtk, timeout=1.0)
            rendered = terminal.get_text_format(Vte.Format.TEXT) or ""
            marker = "│ VISIBLE_MARKER … ─"
            if marker not in rendered:
                raise AssertionError(f"real VTE did not render {marker!r}: {rendered!r}")
            adjustment = terminal.get_vadjustment()
            if adjustment.get_upper() <= adjustment.get_page_size():
                raise AssertionError("real VTE did not retain the replayed history range")
            for bad in ("â¢", "â", "â", "â¦", "â"):
                if bad in rendered:
                    raise AssertionError(f"real VTE rendered replay mojibake {bad!r}")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "direct_sha256": hashlib.sha256(direct).hexdigest(),
                        "replay_sha256": hashlib.sha256(replay).hexdigest(),
                        "replay_bytes": len(replay),
                        "history_utf8_preserved": True,
                        "visible_utf8_preserved": True,
                        "real_vte_text_preserved": True,
                        "mojibake_absent": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            if controller is not None:
                controller.stop()
            try:
                window.destroy()
            except Exception:
                pass
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )


if __name__ == "__main__":
    raise SystemExit(main())
