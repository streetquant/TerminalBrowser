from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import time

import pytest

from termdeck import storage_fabric
from termdeck.session_recipe import build_session_recipe, encode_session_recipe
from termdeck.session_store import SessionStore
from termdeck.terminal_launch_environment import ACCOUNT_SELECTORS, bind_account_environment
from termdeck.tmux_backend import TmuxBackend


def _start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _selectors(pid):
    return {os.fsdecode(k): os.fsdecode(v) for value in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if b"=" in value for k, v in [value.split(b"=", 1)] if os.fsdecode(k) in ACCOUNT_SELECTORS}


def test_opaque_command_remains_exact_with_explicit_empty_and_overrides(tmp_path):
    command = "printf '%s' \"$CODEX_HOME\" > " + shlex.quote(str(tmp_path / "owned"))
    argv = bind_account_environment(command, {"CODEX_HOME": "recipe home", "OPENAI_BASE_URL": ""})
    assert argv[-1] == command
    inherited = dict(os.environ, **{key: "stale-owned-control" for key in ACCOUNT_SELECTORS})
    subprocess.run(argv, env=inherited, check=True, capture_output=True, timeout=3)
    assert (tmp_path / "owned").read_text() == "recipe home"
    assert "OPENAI_BASE_URL=" in argv


@pytest.mark.skipif(not Path("/usr/bin/tmux").is_file(), reason="tmux unavailable")
def test_respawn_uses_recipe_tool_path_instead_of_client_or_server(tmp_path):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    tool = tool_dir / "owned-launch-probe"
    tool.write_text("#!/bin/sh\nprintf '%s\\n' recipe-tool-ready\n")
    tool.chmod(0o700)
    output = tmp_path / "probe.txt"
    socket_path = tmp_path / "owned.sock"
    base = ["/usr/bin/tmux", "-S", str(socket_path)]
    client = dict(os.environ, PATH="/usr/bin:/bin")
    try:
        subprocess.run(base + ["new-session", "-d", "-s", "owned-path", "/usr/bin/sleep", "60"],
                       env=client, check=True, capture_output=True, timeout=5)
        command = f"owned-launch-probe > {shlex.quote(str(output))}; /usr/bin/sleep 60"
        environment = {"PATH": str(tool_dir) + ":/usr/bin:/bin", "HOME": str(tmp_path)}
        subprocess.run(base + ["respawn-pane", "-k", "-t", "owned-path",
                              *bind_account_environment(command, environment)],
                       env=client, check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not output.exists() or not output.read_text()):
            time.sleep(0.02)
        assert output.read_text().strip() == "recipe-tool-ready"
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, timeout=5)


@pytest.mark.skipif(not Path("/usr/bin/tmux").is_file(), reason="tmux unavailable")
def test_two_real_sessions_mask_stale_server_selectors_and_preserve_explicit_recipe(tmp_path, monkeypatch):
    root = tmp_path / "tm"
    root.mkdir(mode=0o700)
    socket_root = root / f"tmux-{os.geteuid()}"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "default"
    store = SessionStore(tmp_path / "sessions.sqlite3", provision_auth_scopes=False)
    server_pid = server_start = None
    evidence = {}
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    for key in ("TMUX", "TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_TMUX_RUNTIME_BINDING", None)
    monkeypatch.setattr(storage_fabric, "_PRODUCTION_DATABASE_BINDING", None)
    # A raw owned seed establishes the bad first-caller environment. The two
    # later sessions use the actual backend boundary, without terminal input.
    seed_env = dict(os.environ, **{name: "stale-owned-control" for name in ACCOUNT_SELECTORS})
    try:
        subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "new-session", "-d", "-s", "owned-seed",
                        "/usr/bin/bash --noprofile --norc -i"], env=seed_env,
                       check=True, capture_output=True, timeout=5)
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.connect(str(socket_path))
            server_pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        finally:
            peer.close()
        assert uid == os.geteuid()
        server_start = _start(server_pid)
        evidence.update(server_pid=server_pid, server_start=server_start)
        assert server_start is not None
        for name in ACCOUNT_SELECTORS:
            monkeypatch.delenv(name, raising=False)
        first = store.create("negative", "owned", "/usr/bin/bash --noprofile --norc -i", str(tmp_path))
        second = store.create("positive", "owned", "/usr/bin/bash --noprofile --norc -i", str(tmp_path))
        recipe = build_session_recipe(second.command, second.cwd, environment={"CODEX_HOME": "recipe-owned-control",
                                        "OPENAI_API_KEY": "", "CODEX_SQLITE_HOME": "recipe-sqlite-control"})
        encoded, digest = encode_session_recipe(recipe)
        second = store.update(second.id, recovery_recipe=encoded, recovery_recipe_sha256=digest)
        backend = TmuxBackend(executable="/usr/bin/tmux")
        backend.create_or_resume(first)
        backend.create_or_resume(second)
        observations = backend.reconciliation_snapshot([first, second])
        evidence["panes"] = {key: {"pid": item["pid"], "start_time": item["start_time"]} for key, item in observations.items()}
        assert all(item["state"] == "live" for item in observations.values())
        # tmux reports the forked PID before env has exec'd the terminal.
        # Require the same process generation to reach its actual Bash payload.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if all(Path(f"/proc/{item['pid']}/exe").resolve().name == "bash" and
                   Path(f"/proc/{item['pid']}/cmdline").read_bytes().split(b"\0")[1:4] ==
                   [b"--noprofile", b"--norc", b"-i"] for item in observations.values()):
                break
            time.sleep(0.02)
        evidence["payloads"] = {key: {
            "exe": str(Path(f"/proc/{item['pid']}/exe").resolve()),
            "argv": [os.fsdecode(value) for value in Path(f"/proc/{item['pid']}/cmdline").read_bytes().split(b"\0") if value]
        } for key, item in observations.items()}
        evidence["selector_presence"] = {key: sorted(_selectors(item["pid"])) for key, item in observations.items()}
        assert _selectors(observations[first.id]["pid"]) == {}
        assert _selectors(observations[second.id]["pid"]) == {
            "CODEX_HOME": "recipe-owned-control", "OPENAI_API_KEY": "", "CODEX_SQLITE_HOME": "recipe-sqlite-control"}
        # Server selectors remain untouched: only the new processes are scoped.
        assert _selectors(server_pid) == {name: "stale-owned-control" for name in ACCOUNT_SELECTORS}
        before = [(item["pid"], item["start_time"]) for item in observations.values()]
        backend.create_or_resume(first)
        backend.create_or_resume(second)
        assert before == [(item["pid"], item["start_time"]) for item in backend.reconciliation_snapshot([first, second]).values()]
    finally:
        store.close()
        if server_pid is not None and _start(server_pid) == server_start:
            subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "kill-server"],
                           check=False, capture_output=True, timeout=3)
            deadline = time.monotonic() + 3
            while _start(server_pid) == server_start and time.monotonic() < deadline:
                time.sleep(0.02)
            evidence["owned_server_generation_gone"] = _start(server_pid) != server_start
            (tmp_path / "owned-environment-evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")
            assert evidence["owned_server_generation_gone"], "owned disposable server survived cleanup"
