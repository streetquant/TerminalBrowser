from __future__ import annotations

import json
import os
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from termdeck.pane_stream import PaneStreamController
from termdeck.resize_signal import (
    private_launch_tuple_for_scope,
    propagate_terminal_resize,
)


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not satisfied before timeout")


def test_control_resize_repairs_contained_tui_before_replay(tmp_path: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is unavailable")
    socket = f"termdeck-resize-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    tmux_tmp = Path(tempfile.mkdtemp(prefix="termdeck-resize-", dir=os.environ.get("TMPDIR")))
    tmux_tmp.chmod(0o700)
    env = os.environ.copy()
    env["TMUX_TMPDIR"] = str(tmux_tmp)

    target = tmp_path / "target.py"
    inner = tmp_path / "inner.py"
    outer = tmp_path / "outer.py"
    wrapper = tmp_path / "wrapper.sh"
    payload_ready = tmp_path / "payload.ready"
    mcp_ready = tmp_path / "mcp.ready"
    ready = tmp_path / "ready.json"
    mcp_receipt = tmp_path / "mcp.received"
    auth_scope_id = "auth-0123456789abcdef0123456789abcdef"
    codex_home = tmp_path / auth_scope_id
    codex_home.mkdir(mode=0o700)
    expected_private_tuple = private_launch_tuple_for_scope(
        str(codex_home),
        auth_scope_id,
    )

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
    sys.stdout.write('⚠                                       \\r\\n')
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
mcp = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[3], sys.argv[4], 'mcp'], preexec_fn=os.setpgrp)
for _attempt in range(500):
    if Path(sys.argv[2]).exists() and Path(sys.argv[3]).exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(97)
Path(sys.argv[5]).write_text(json.dumps({'inner': os.getpid(), 'payload': payload.pid, 'mcp': mcp.pid}), encoding='utf-8')
raise SystemExit(payload.wait())
""",
        encoding="utf-8",
    )
    outer.write_text(
        """import ctypes, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'bwrap', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
inner = subprocess.Popen([sys.executable, sys.argv[1], *sys.argv[2:]], start_new_session=True)
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
                str(inner),
                str(target),
                str(payload_ready),
                str(mcp_ready),
                str(mcp_receipt),
                str(ready),
                str(codex_home),
                auth_scope_id,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    outputs: list[bytes] = []
    controller: PaneStreamController | None = None
    identities: dict[str, int] = {}
    try:
        created = subprocess.run(
            [
                tmux,
                "-L",
                socket,
                "new-session",
                "-d",
                "-x",
                "100",
                "-y",
                "40",
                "-s",
                "demo",
                str(wrapper),
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        _wait(ready.exists)
        identities = {key: int(value) for key, value in json.loads(ready.read_text()).items()}

        controller = PaneStreamController(
            attach_argv=[tmux, "-L", socket, "attach-session", "-t", "demo"],
            environment=env,
            on_output=outputs.append,
            on_resize_applied=lambda pane_pid, _columns, _rows: propagate_terminal_resize(
                pane_pid,
                expected_private_tuple=expected_private_tuple,
            ),
        )
        controller.start(columns=100, rows=40)
        _wait(lambda: b"COMPOSER_RESTORED" in b"".join(outputs), timeout=15)

        pane = subprocess.run(
            [tmux, "-L", socket, "display-message", "-p", "-t", "demo", "#{pane_pid} #{pane_width}x#{pane_height}"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=True,
        ).stdout.strip()
        pane_pid, grid = pane.split()
        assert int(pane_pid) > 1
        assert grid == "100x40"
        assert Path(f"/proc/{identities['payload']}").exists()
        time.sleep(0.2)
        assert not mcp_receipt.exists()
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
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        shutil.rmtree(tmux_tmp, ignore_errors=True)
