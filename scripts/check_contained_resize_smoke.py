#!/usr/bin/env python3
"""Prove contained Codex composer recovery without input or process restart."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import termdeck
import termdeck.pane_stream as pane_stream_module
import termdeck.resize_signal as resize_signal_module
from termdeck.pane_stream import PaneStreamController
from termdeck.resize_signal import (
    private_launch_tuple_for_scope,
    propagate_terminal_resize,
)
from smoke_support import require_private_validation_runtime


_REQUESTED_COLUMNS = 100
_REQUESTED_ROWS = 40
_WAIT_SECONDS = 15.0


def _wait(predicate, timeout: float = _WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


def _start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="latin-1")
    close = raw.rfind(") ")
    if close < 0:
        raise AssertionError("process stat is malformed")
    fields = raw[close + 2 :].split()
    value = int(fields[19])
    if value <= 0:
        raise AssertionError("process start ticks are invalid")
    return value


def _module_origins() -> tuple[bool, dict[str, str]]:
    modules = {
        "termdeck": Path(termdeck.__file__).resolve(),
        "pane_stream": Path(pane_stream_module.__file__).resolve(),
        "resize_signal": Path(resize_signal_module.__file__).resolve(),
    }
    expected_raw = os.environ.get("T1000_EXPECTED_SITE", "").strip()
    if not expected_raw:
        return False, {key: path.name for key, path in modules.items()}
    expected = Path(expected_raw).resolve(strict=True)
    relative: dict[str, str] = {}
    for key, path in modules.items():
        try:
            relative[key] = str(path.relative_to(expected))
        except ValueError as exc:
            raise AssertionError(f"{key} was not imported from the selected package site") from exc
    return True, relative


def _tmux_message(tmux: str, socket: str, env: dict[str, str], format_string: str) -> str:
    completed = subprocess.run(
        [tmux, "-L", socket, "display-message", "-p", "-t", "demo", format_string],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"tmux display-message failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def main() -> int:
    tmux = shutil.which("tmux")
    if tmux is None:
        print(
            json.dumps(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "tmux is unavailable",
                },
                sort_keys=True,
            )
        )
        return 0

    package_site_bound, module_origins = _module_origins()
    runtime_base = require_private_validation_runtime()
    socket = f"c-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    environment = os.environ.copy()
    environment["TMUX_TMPDIR"] = str(runtime_base)

    controller: PaneStreamController | None = None
    identities: dict[str, int] = {}
    signal_results: list[dict[str, object]] = []
    signal_lock = threading.Lock()
    outputs: list[bytes] = []

    with tempfile.TemporaryDirectory(
        prefix="contained-source-",
        dir=runtime_base,
    ) as raw_root:
        root = Path(raw_root)
        root.chmod(0o700)
        target = root / "target.py"
        inner = root / "inner.py"
        outer = root / "outer.py"
        wrapper = root / "wrapper.sh"
        payload_ready = root / "payload.ready"
        mcp_ready = root / "mcp.ready"
        identity_receipt = root / "identity.json"
        outer_ready = root / "outer.pid"
        mcp_receipt = root / "mcp.received"
        input_marker = root / "terminal-input.received"
        scope = "auth-0123456789abcdef0123456789abcdef"
        codex_home = root / scope
        codex_home.mkdir(mode=0o700)
        expected_private_tuple = private_launch_tuple_for_scope(str(codex_home), scope)

        target.write_text(
            """from pathlib import Path
import ctypes, os, signal, sys
ready = Path(sys.argv[1])
receipt = Path(sys.argv[2]) if sys.argv[2] != '-' else None
label = sys.argv[3]
if label == 'payload':
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(15, b'codex', 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'prctl failed')
def handler(*_args):
    if receipt is not None:
        receipt.write_text(str(os.getpid()), encoding='ascii')
    if label == 'payload':
        sys.stdout.write('\\x1b[2J\\x1b[HCOMPOSER_RESTORED\\r\\n» Explain this codebase\\r\\n')
        sys.stdout.flush()
signal.signal(signal.SIGWINCH, handler)
if label == 'payload':
    # Codex is a full-screen alternate-screen TUI. Model that terminal mode so
    # a repaired clear/redraw replaces the stale frame instead of preserving
    # it as ordinary scrollback, which would test a shell transcript rather
    # than the composer failure reported by the user.
    sys.stdout.write('\\x1b[?1049h\\x1b[2J\\x1b[H⚠ COLLAPSED_COMPOSER_FRAME\\r\\n')
    sys.stdout.flush()
ready.write_text(str(os.getpid()), encoding='ascii')
while True:
    signal.pause()
""",
            encoding="utf-8",
        )
        inner.write_text(
            """import ctypes, json, os, subprocess, sys, time
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'bwrap', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
payload_env = dict(os.environ)
payload_env.update({
    'CODEX_HOME': sys.argv[6],
    'TERMDECK_AUTH_SCOPE_ID': sys.argv[7],
    'CODEX_TERMDECK_GATE_ROOT': sys.argv[6] + '/.termdeck-codex-lifetime',
})
payload = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2], '-', 'payload'],
    env=payload_env,
)
mcp = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[3], sys.argv[4], 'mcp'],
    preexec_fn=os.setpgrp,
)
for _attempt in range(500):
    if Path(sys.argv[2]).exists() and Path(sys.argv[3]).exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(97)
Path(sys.argv[5]).write_text(
    json.dumps({'inner': os.getpid(), 'payload': payload.pid, 'mcp': mcp.pid}),
    encoding='utf-8',
)
raise SystemExit(payload.wait())
""",
            encoding="utf-8",
        )
        outer.write_text(
            """import ctypes, subprocess, sys
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'bwrap', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='ascii')
inner = subprocess.Popen(
    [sys.executable, sys.argv[2], *sys.argv[3:]],
    start_new_session=True,
)
raise SystemExit(inner.wait())
""",
            encoding="utf-8",
        )
        wrapper.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            + shlex.join(
                (
                    sys.executable,
                    str(outer),
                    str(outer_ready),
                    str(inner),
                    str(target),
                    str(payload_ready),
                    str(mcp_ready),
                    str(mcp_receipt),
                    str(identity_receipt),
                    str(codex_home),
                    scope,
                )
            )
            + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)

        def resize_callback(pane_pid: int, columns: int, rows: int) -> object:
            result = propagate_terminal_resize(
                pane_pid,
                expected_private_tuple=expected_private_tuple,
            )
            projection = {
                "signalled": result.signalled,
                "signal_method": result.signal_method,
                "target_pid": result.target_pid,
                "target_pgrp": result.target_pgrp,
                "witness_pid": result.witness_pid,
                "scanned_processes": result.scanned_processes,
                "columns": int(columns),
                "rows": int(rows),
            }
            with signal_lock:
                if len(signal_results) < 16:
                    signal_results.append(projection)
            return result

        try:
            created = subprocess.run(
                [
                    tmux,
                    "-L",
                    socket,
                    "new-session",
                    "-d",
                    "-x",
                    str(_REQUESTED_COLUMNS),
                    "-y",
                    str(_REQUESTED_ROWS),
                    "-s",
                    "demo",
                    str(wrapper),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
            if created.returncode != 0:
                raise AssertionError(f"tmux new-session failed: {created.stderr.strip()}")
            _wait(lambda: identity_receipt.exists() and outer_ready.exists())
            identities = {
                key: int(value)
                for key, value in json.loads(identity_receipt.read_text()).items()
            }
            identities["outer"] = int(outer_ready.read_text())
            pane_before = _tmux_message(
                tmux,
                socket,
                environment,
                "#{pane_pid} #{pane_width}x#{pane_height} #{window_width}x#{window_height}",
            ).split()
            identities["pane"] = int(pane_before[0])
            start_before = {key: _start_ticks(pid) for key, pid in identities.items()}

            controller = PaneStreamController(
                attach_argv=[
                    tmux,
                    "-L",
                    socket,
                    "attach-session",
                    "-t",
                    "demo",
                ],
                environment=environment,
                on_output=outputs.append,
                on_resize_applied=resize_callback,
            )

            def forbidden_input(*_args: object, **_kwargs: object) -> bool:
                input_marker.write_text("unexpected terminal input", encoding="utf-8")
                raise AssertionError("composer recovery emitted terminal input")

            controller._send_input_bytes = forbidden_input  # type: ignore[method-assign]
            controller.start(columns=_REQUESTED_COLUMNS, rows=_REQUESTED_ROWS)
            _wait(
                lambda: b"COMPOSER_RESTORED" in b"".join(outputs)
                and bool(getattr(controller, "_ready", False))
            )
            joined = b"".join(outputs)
            if b"COLLAPSED_COMPOSER_FRAME" in joined:
                raise AssertionError("collapsed composer frame leaked into committed replay")
            if input_marker.exists():
                raise AssertionError("composer recovery invoked terminal input")

            pane_after = _tmux_message(
                tmux,
                socket,
                environment,
                "#{pane_pid} #{pane_width}x#{pane_height} #{window_width}x#{window_height}",
            ).split()
            if int(pane_after[0]) != identities["pane"]:
                raise AssertionError("tmux pane root changed during composer recovery")
            expected_grid = f"{_REQUESTED_COLUMNS}x{_REQUESTED_ROWS}"
            if pane_after[1] != expected_grid or pane_after[2] != expected_grid:
                raise AssertionError(f"final tmux grid differed: {pane_after[1:]}")
            if tuple(controller._size) != (
                _REQUESTED_COLUMNS,
                _REQUESTED_ROWS,
            ):
                raise AssertionError("stream controller grid differed from tmux")

            start_after = {key: _start_ticks(pid) for key, pid in identities.items()}
            if start_after != start_before:
                raise AssertionError("one or more protected process identities changed")
            time.sleep(0.2)
            if mcp_receipt.exists():
                raise AssertionError("MCP-like sibling received containment signal")
            with signal_lock:
                observed_signals = list(signal_results)
            successful = [entry for entry in observed_signals if entry["signalled"]]
            if not successful:
                raise AssertionError("no contained resize signal succeeded")
            pidfd_capable = callable(getattr(os, "pidfd_open", None)) and callable(
                getattr(signal, "pidfd_send_signal", None)
            )
            if pidfd_capable and not any(
                entry["signal_method"] == "pidfd-witness" for entry in successful
            ):
                raise AssertionError("capable host did not use pidfd witness signaling")
            if any(entry["witness_pid"] != identities["payload"] for entry in successful):
                raise AssertionError("resize signal targeted an unexpected witness")
            if any(entry["target_pid"] != identities["inner"] for entry in successful):
                raise AssertionError("resize signal targeted an unexpected inner session")

            payload = {
                "ok": True,
                "schema": "t1000.contained-resize-smoke.v1",
                "version": termdeck.__version__,
                "package_site_bound": package_site_bound,
                "module_origins": module_origins,
                "requested_grid": expected_grid,
                "pane_grid": pane_after[1],
                "window_grid": pane_after[2],
                "controller_grid": f"{controller._size[0]}x{controller._size[1]}",
                "composer_restored_before_ready": True,
                "collapsed_frame_replayed": False,
                "terminal_input_calls": 0,
                "protected_process_count": len(identities),
                "protected_identities_preserved": True,
                "mcp_signal_count": 0,
                "signal_methods": sorted(
                    {str(entry["signal_method"]) for entry in successful}
                ),
                "signal_count": len(successful),
                "pidfd_capable": pidfd_capable,
                "output_sha256": hashlib.sha256(joined).hexdigest(),
            }
            print(json.dumps(payload, sort_keys=True))
            return 0
        finally:
            if controller is not None:
                controller.stop()
            inner_pid = identities.get("inner")
            mcp_pid = identities.get("mcp")
            if inner_pid:
                try:
                    os.killpg(inner_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if mcp_pid:
                try:
                    os.killpg(mcp_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            subprocess.run(
                [tmux, "-L", socket, "kill-server"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
