#!/usr/bin/env python3
"""Run the inherited 120-shard plus 25-smoke TermDeck web profile safely."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import sqlite3
import statistics
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websocket

HTTP_SHARDS = 120
WEBSOCKET_SMOKES = 25


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as caught:  # noqa: BLE001 - bounded readiness polling
            error = caught
        time.sleep(0.05)
    raise RuntimeError(f"server readiness timeout: {error}")


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def tmux(socket_name: str, *arguments: str, timeout: float = 20.0) -> str:
    return subprocess.check_output(
        ["/usr/bin/tmux", "-L", socket_name, *arguments],
        text=True,
        timeout=timeout,
    )


def registry(path: Path, tmux_name: str, session_id: str, cwd: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL,
              tmux_name TEXT NOT NULL, status TEXT NOT NULL, window_id TEXT NOT NULL,
              window_title TEXT NOT NULL, tab_index INTEGER NOT NULL,
              visible INTEGER NOT NULL, pinned INTEGER NOT NULL,
              last_opened_at TEXT NOT NULL, pane_target TEXT
            );
            CREATE TABLE remembered_windows (
              window_id TEXT PRIMARY KEY, window_title TEXT NOT NULL
            );
            INSERT INTO remembered_windows VALUES ('win-profile', 'Random');
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                "Production profile",
                str(cwd),
                tmux_name,
                "running",
                "win-profile",
                "Random",
                0,
                1,
                0,
                "2026-08-28T00:00:00Z",
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def wrappers(root: Path, socket_name: str) -> tuple[Path, Path]:
    tmux_wrapper = root / "tmux"
    tmux_wrapper.write_text(
        f'#!/bin/sh\nexec /usr/bin/tmux -L {socket_name} "$@"\n', encoding="utf-8"
    )
    tmux_wrapper.chmod(0o700)
    t1000_wrapper = root / "t1000"
    t1000_wrapper.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"ok\":false}'\nexit 1\n", encoding="utf-8"
    )
    t1000_wrapper.chmod(0o700)
    return tmux_wrapper, t1000_wrapper


def invitation(binary: Path, auth_dir: Path, host_id: str, owner_id: str, label: str) -> str:
    payload = json.loads(
        subprocess.check_output(
            [
                str(binary),
                "--auth-dir",
                str(auth_dir),
                "--host-id",
                host_id,
                "--owner-id",
                owner_id,
                "--create-invitation",
                label,
            ],
            text=True,
            timeout=30,
        )
    )
    return str(payload["invitation_secret"])


def pair(origin: str, secret: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        f"{origin}/_termdeck/pair",
        data=urllib.parse.urlencode({"invitation": secret}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
        },
    )
    with opener.open(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"pairing failed: HTTP {response.status}")
    cookie = "; ".join(f"{item.name}={item.value}" for item in jar)
    if "termdeck_session=" not in cookie or "termdeck_csrf=" not in cookie:
        raise RuntimeError("pairing cookie set was incomplete")
    return cookie


def server_command(
    binary: Path,
    database: Path,
    port: int,
    tmux_wrapper: Path,
    t1000_wrapper: Path,
    host_id: str,
    owner_id: str,
    auth_dir: Path | None,
) -> list[str]:
    command = [
        str(binary),
        "--db",
        str(database),
        "--listen",
        f"127.0.0.1:{port}",
        "--refresh-ms",
        "250",
        "--max-stale-ms",
        "10000",
        "--max-connections",
        "256",
        "--tmux",
        str(tmux_wrapper),
        "--tmux-timeout-ms",
        "3000",
        "--t1000",
        str(t1000_wrapper),
        "--host-id",
        host_id,
        "--owner-id",
        owner_id,
    ]
    if auth_dir is not None:
        command.extend(
            ["--auth-dir", str(auth_dir), "--allow-insecure-loopback-auth"]
        )
    return command


def http_profile(binary: Path, root: Path, evidence: Path) -> dict[str, Any]:
    fixture = Path(tempfile.mkdtemp(prefix="termdeck-http-shards-", dir=root / ".ai-bridge" / "tmp"))
    socket_name = f"termdeck-http-shards-{os.getpid()}-{time.time_ns()}"
    tmux_name = "termdeck_profile_http"
    session_id = "td-profile-http"
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[Any] | None = None
    try:
        tmux_wrapper, t1000_wrapper = wrappers(fixture, socket_name)
        subprocess.run(
            [
                "/usr/bin/tmux",
                "-L",
                socket_name,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                tmux_name,
                "-x",
                "100",
                "-y",
                "30",
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
            ],
            check=True,
            timeout=10,
        )
        database = fixture / "sessions.sqlite3"
        registry(database, tmux_name, session_id, root)
        stdout = (evidence / "http-server.stdout").open("wb")
        stderr = (evidence / "http-server.stderr").open("wb")
        process = subprocess.Popen(
            server_command(
                binary,
                database,
                port,
                tmux_wrapper,
                t1000_wrapper,
                "host-http-profile",
                "owner-http-profile",
                None,
            ),
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        stdout.close()
        stderr.close()
        wait_http(f"{origin}/_termdeck/healthz")
        endpoints = (
            "/",
            "/random",
            "/_termdeck/api/v1/snapshot",
            "/_termdeck/api/v1/windows/random",
            "/_termdeck/assets/v2/app.js",
            "/_termdeck/assets/v2/app.css",
            "/_termdeck/healthz",
            "/_termdeck/readyz",
        )

        def shard(index: int) -> dict[str, Any]:
            latencies: list[float] = []
            static_hashes: dict[str, str] = {}
            for endpoint in endpoints:
                started = time.monotonic()
                request = urllib.request.Request(
                    f"{origin}{endpoint}", headers={"Accept": "*/*", "X-Profile-Shard": str(index)}
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read(20 * 1024 * 1024)
                    status = response.status
                    content_type = response.headers.get("Content-Type", "")
                latencies.append((time.monotonic() - started) * 1000.0)
                if status != 200:
                    raise RuntimeError(f"shard {index} endpoint {endpoint} returned {status}")
                if endpoint.endswith(("app.js", "app.css")):
                    static_hashes[endpoint] = hashlib.sha256(body).hexdigest()
                elif "json" in content_type:
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise RuntimeError(f"shard {index} endpoint {endpoint} returned invalid JSON")
                    if endpoint.startswith("/_termdeck/api/") and "schema" not in payload:
                        raise RuntimeError(
                            f"shard {index} endpoint {endpoint} omitted its versioned schema"
                        )
                    if endpoint.endswith(("healthz", "readyz")) and payload.get("ok") is not True:
                        raise RuntimeError(
                            f"shard {index} endpoint {endpoint} did not report ready health"
                        )
            return {
                "index": index,
                "max_ms": max(latencies),
                "sum_ms": sum(latencies),
                "hashes": static_hashes,
            }

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
            shards = list(executor.map(shard, range(HTTP_SHARDS)))
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if len(shards) != HTTP_SHARDS:
            raise RuntimeError("HTTP shard count mismatch")
        expected_hashes = shards[0]["hashes"]
        if any(item["hashes"] != expected_hashes for item in shards):
            raise RuntimeError("versioned asset bytes differed between shards")
        maxima = sorted(float(item["max_ms"]) for item in shards)
        p95 = maxima[min(len(maxima) - 1, int(len(maxima) * 0.95))]
        if p95 > 2_500 or max(maxima) > 5_000:
            raise RuntimeError(f"HTTP load latency exceeded bounds: p95={p95} max={max(maxima)}")
        return {
            "shards": len(shards),
            "requests": len(shards) * len(endpoints),
            "elapsed_ms": round(elapsed_ms, 3),
            "p50_max_request_ms": round(statistics.median(maxima), 3),
            "p95_max_request_ms": round(p95, 3),
            "maximum_request_ms": round(max(maxima), 3),
            "asset_hashes": expected_hashes,
        }
    finally:
        terminate(process)
        subprocess.run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        shutil.rmtree(fixture, ignore_errors=True)


def receive_ready(connection: websocket.WebSocket) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    ready: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        frame = connection.recv()
        if isinstance(frame, bytes):
            continue
        payload = json.loads(frame)
        if payload.get("type") == "error":
            raise RuntimeError(f"terminal stream error: {payload!r}")
        if payload.get("type") == "ready":
            ready = payload
            break
    if ready is None:
        raise RuntimeError("terminal stream did not become ready")
    return ready


def receive_ack(connection: websocket.WebSocket, sequence: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        frame = connection.recv()
        if isinstance(frame, bytes):
            continue
        payload = json.loads(frame)
        if payload.get("type") == "error":
            raise RuntimeError(f"terminal input error: {payload!r}")
        if payload.get("type") == "ack" and payload.get("sequence") == sequence:
            return
    raise RuntimeError("terminal input acknowledgement timed out")


def websocket_smoke(binary: Path, root: Path, evidence: Path, index: int) -> dict[str, Any]:
    fixture = Path(tempfile.mkdtemp(prefix=f"termdeck-ws-smoke-{index:02d}-", dir=root / ".ai-bridge" / "tmp"))
    socket_name = f"termdeck-ws-smoke-{index}-{os.getpid()}-{time.time_ns()}"
    tmux_name = f"termdeck_profile_ws_{index:02d}"
    session_id = f"td-profile-ws-{index:02d}"
    host_id = f"host-ws-profile-{index:02d}"
    owner_id = f"owner-ws-profile-{index:02d}"
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[Any] | None = None
    connection: websocket.WebSocket | None = None
    try:
        tmux_wrapper, t1000_wrapper = wrappers(fixture, socket_name)
        subprocess.run(
            [
                "/usr/bin/tmux",
                "-L",
                socket_name,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                tmux_name,
                "-x",
                "100",
                "-y",
                "30",
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
            ],
            check=True,
            timeout=10,
        )
        database = fixture / "sessions.sqlite3"
        registry(database, tmux_name, session_id, root)
        auth_parent = fixture / "auth-parent"
        auth_parent.mkdir(mode=0o700)
        auth_dir = auth_parent / "auth"
        secret = invitation(binary, auth_dir, host_id, owner_id, f"Smoke {index}")
        stdout = (evidence / f"ws-{index:02d}.stdout").open("wb")
        stderr = (evidence / f"ws-{index:02d}.stderr").open("wb")
        started = time.monotonic()
        process = subprocess.Popen(
            server_command(
                binary,
                database,
                port,
                tmux_wrapper,
                t1000_wrapper,
                host_id,
                owner_id,
                auth_dir,
            ),
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        stdout.close()
        stderr.close()
        wait_http(f"{origin}/_termdeck/healthz")
        cookie = pair(origin, secret)
        connection = websocket.create_connection(
            f"ws://127.0.0.1:{port}/_termdeck/ws/v1/windows/random/panes/{session_id}",
            subprotocols=["termdeck.terminal.v1"],
            cookie=cookie,
            origin=origin,
            timeout=15,
            enable_multithread=False,
        )
        connection.send(json.dumps({"type": "hello", "cols": 100, "rows": 30}))
        ready = receive_ready(connection)
        control = ready.get("control")
        if not isinstance(control, dict):
            raise RuntimeError("ready frame lacked controller metadata")
        connection.send(json.dumps({"type": "focus"}))
        sequence = control.get("next_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            # Focus can emit a control frame with the first sequence. Read until available.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                frame = connection.recv()
                if isinstance(frame, str):
                    payload = json.loads(frame)
                    candidate = payload.get("control")
                    if isinstance(candidate, dict) and isinstance(candidate.get("next_sequence"), int):
                        sequence = candidate["next_sequence"]
                        break
        if not isinstance(sequence, int) or sequence < 1:
            raise RuntimeError("controller sequence was unavailable")
        marker = f"PROFILE_SMOKE_{index:02d}_OK"
        input_bytes = f"printf '{marker}\\n'\n".encode()
        frame = b"\x01" + struct.pack(">Q", sequence) + input_bytes
        connection.send_binary(frame)
        receive_ack(connection, sequence)
        deadline = time.monotonic() + 10
        captured = ""
        while time.monotonic() < deadline:
            captured = tmux(socket_name, "capture-pane", "-epq", "-t", f"{tmux_name}:0.0", "-S", "-200")
            if marker in captured:
                break
            time.sleep(0.1)
        if marker not in captured:
            raise RuntimeError(f"smoke marker {marker} did not reach the disposable pane")
        return {
            "index": index,
            "marker": marker,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
    finally:
        if connection is not None:
            connection.close()
        terminate(process)
        subprocess.run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        shutil.rmtree(fixture, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    binary = args.binary.resolve()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if not binary.is_file():
        raise RuntimeError(f"server binary is absent: {binary}")
    (root / ".ai-bridge" / "tmp").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    http = http_profile(binary, root, evidence)
    smokes = [websocket_smoke(binary, root, evidence, index) for index in range(WEBSOCKET_SMOKES)]
    if http["shards"] != HTTP_SHARDS or len(smokes) != WEBSOCKET_SMOKES:
        raise RuntimeError("production profile cardinality mismatch")
    payload = {
        "schema": "termdeck.production-validation-profile.v1",
        "ok": True,
        "http": http,
        "websocket_smokes": smokes,
        "shards": HTTP_SHARDS,
        "smokes": WEBSOCKET_SMOKES,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    (evidence / "result.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
