#!/usr/bin/env python3
"""Prove an isolated HTTPS LAN reverse proxy upgrades TermDeck WebSockets."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind((host, 0))
        return int(stream.getsockname()[1])


def lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        try:
            datagram.connect(("1.1.1.1", 53))
            value = str(datagram.getsockname()[0])
        except OSError:
            value = "127.0.0.1"
    if value.startswith("127."):
        output = subprocess.check_output(
            ["/usr/bin/hostname", "-I"], text=True, timeout=5
        ).split()
        value = next((item for item in output if not item.startswith("127.")), value)
    if value.startswith("127."):
        raise RuntimeError("no non-loopback LAN address is available")
    return value


def wait_url(url: str, context: Any = None, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if context is None:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            else:
                response = context.get(url, timeout=1_000, fail_on_status_code=False)
                if response.status == 200:
                    return
        except Exception as caught:  # noqa: BLE001 - bounded readiness probe
            error = caught
        time.sleep(0.05)
    raise RuntimeError(f"readiness timed out for {url}: {error}")


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def registry(path: Path, tmux_name: str, cwd: Path) -> None:
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
            INSERT INTO remembered_windows VALUES ('win-proxy', 'Random');
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "td-proxy",
                "HTTPS proxy fixture",
                str(cwd),
                tmux_name,
                "running",
                "win-proxy",
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


def invitation(binary: Path, auth_dir: Path) -> str:
    output = subprocess.check_output(
        [
            str(binary),
            "--auth-dir",
            str(auth_dir),
            "--host-id",
            "host-proxy",
            "--owner-id",
            "owner-proxy",
            "--create-invitation",
            "LAN Chromium",
        ],
        text=True,
        timeout=30,
    )
    return str(json.loads(output)["invitation_secret"])


def pair(origin: str, secret: str, domain: str) -> list[dict[str, Any]]:
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
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain,
            "path": "/",
            "httpOnly": cookie.name.endswith("session"),
            "secure": False,
            "sameSite": "Strict",
        }
        for cookie in jar
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    binary = args.binary.resolve()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    caddy = shutil.which("caddy")
    nginx = shutil.which("nginx")
    openssl = shutil.which("openssl")
    if caddy is None and (nginx is None or openssl is None):
        raise RuntimeError(
            "the HTTPS WebSocket proof requires caddy, or both nginx and openssl"
        )

    temp_root = Path(tempfile.mkdtemp(prefix="termdeck-https-proxy-", dir=root / ".ai-bridge" / "tmp"))
    socket_name = f"termdeck-proxy-{os.getpid()}-{time.time_ns()}"
    tmux_name = "termdeck_proxy_fixture"
    backend_port = free_port("127.0.0.1")
    address = lan_address()
    frontend_port = free_port(address)
    backend_origin = f"http://127.0.0.1:{backend_port}"
    frontend_origin = f"https://{address}:{frontend_port}"
    database = temp_root / "sessions.sqlite3"
    auth_parent = temp_root / "auth-parent"
    auth_parent.mkdir(mode=0o700)
    auth_dir = auth_parent / "auth"
    tmux_wrapper = temp_root / "tmux"
    t1000_wrapper = temp_root / "t1000"
    proxy_home = temp_root / "proxy-home"
    proxy_home.mkdir(mode=0o700)
    proxy_config = temp_root / ("Caddyfile" if caddy is not None else "nginx.conf")
    certificate = temp_root / "proxy.crt"
    private_key = temp_root / "proxy.key"
    proxy_kind = "caddy" if caddy is not None else "nginx"
    backend: subprocess.Popen[Any] | None = None
    proxy: subprocess.Popen[Any] | None = None
    browser: Any = None
    context: Any = None
    try:
        tmux_wrapper.write_text(
            f'#!/bin/sh\nexec /usr/bin/tmux -L {socket_name} "$@"\n', encoding="utf-8"
        )
        tmux_wrapper.chmod(0o700)
        t1000_wrapper.write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"ok\":false}'\nexit 1\n", encoding="utf-8"
        )
        t1000_wrapper.chmod(0o700)
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
        registry(database, tmux_name, root)
        secret = invitation(binary, auth_dir)
        backend_stdout = (evidence / "backend.stdout").open("wb")
        backend_stderr = (evidence / "backend.stderr").open("wb")
        backend = subprocess.Popen(
            [
                str(binary),
                "--db",
                str(database),
                "--listen",
                f"127.0.0.1:{backend_port}",
                "--refresh-ms",
                "250",
                "--max-stale-ms",
                "10000",
                "--tmux",
                str(tmux_wrapper),
                "--tmux-timeout-ms",
                "3000",
                "--t1000",
                str(t1000_wrapper),
                "--host-id",
                "host-proxy",
                "--owner-id",
                "owner-proxy",
                "--auth-dir",
                str(auth_dir),
                "--allow-insecure-loopback-auth",
            ],
            cwd=root,
            stdout=backend_stdout,
            stderr=backend_stderr,
            start_new_session=True,
        )
        backend_stdout.close()
        backend_stderr.close()
        wait_url(f"{backend_origin}/_termdeck/healthz")
        cookies = pair(backend_origin, secret, address)
        environment = os.environ.copy()
        if caddy is not None:
            proxy_config.write_text(
                f"""https://{address}:{frontend_port} {{
    tls internal
    reverse_proxy 127.0.0.1:{backend_port} {{
        header_up Host 127.0.0.1:{backend_port}
        header_up Origin http://127.0.0.1:{backend_port}
        header_up X-Forwarded-Proto https
        transport http {{
            versions h1
        }}
    }}
}}
""",
                encoding="utf-8",
            )
            environment.update(
                {
                    "HOME": str(proxy_home),
                    "XDG_DATA_HOME": str(proxy_home / "data"),
                    "XDG_CONFIG_HOME": str(proxy_home / "config"),
                }
            )
            validate_command = [
                caddy,
                "validate",
                "--config",
                str(proxy_config),
                "--adapter",
                "caddyfile",
            ]
            proxy_command = [
                caddy,
                "run",
                "--config",
                str(proxy_config),
                "--adapter",
                "caddyfile",
            ]
        else:
            if nginx is None or openssl is None:
                raise RuntimeError("nginx/openssl proxy selection became inconsistent")
            for name in ("client-body", "proxy", "fastcgi", "uwsgi", "scgi"):
                (proxy_home / name).mkdir(mode=0o700)
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-sha256",
                    "-nodes",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                    "-subj",
                    f"/CN={address}",
                    "-addext",
                    f"subjectAltName=IP:{address}",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            private_key.chmod(0o600)
            proxy_config.write_text(
                f"""worker_processes 1;
pid {proxy_home}/nginx.pid;
error_log stderr info;
events {{
    worker_connections 256;
}}
http {{
    access_log off;
    client_body_temp_path {proxy_home}/client-body;
    proxy_temp_path {proxy_home}/proxy;
    fastcgi_temp_path {proxy_home}/fastcgi;
    uwsgi_temp_path {proxy_home}/uwsgi;
    scgi_temp_path {proxy_home}/scgi;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {address}:{frontend_port} ssl;
        server_name {address};
        ssl_certificate {certificate};
        ssl_certificate_key {private_key};
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_cache off;
        client_max_body_size 2m;
        location / {{
            proxy_pass http://127.0.0.1:{backend_port};
            proxy_http_version 1.1;
            proxy_set_header Host 127.0.0.1:{backend_port};
            proxy_set_header Origin http://127.0.0.1:{backend_port};
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
            proxy_read_timeout 1h;
            proxy_send_timeout 1h;
            proxy_buffering off;
        }}
    }}
}}
""",
                encoding="utf-8",
            )
            validate_command = [
                nginx,
                "-p",
                f"{proxy_home}/",
                "-c",
                str(proxy_config),
                "-t",
            ]
            proxy_command = [
                nginx,
                "-p",
                f"{proxy_home}/",
                "-c",
                str(proxy_config),
                "-g",
                "daemon off;",
            ]
        subprocess.run(
            validate_command,
            check=True,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        proxy_stdout = (evidence / f"{proxy_kind}.stdout").open("wb")
        proxy_stderr = (evidence / f"{proxy_kind}.stderr").open("wb")
        proxy = subprocess.Popen(
            proxy_command,
            stdout=proxy_stdout,
            stderr=proxy_stderr,
            env=environment,
            start_new_session=True,
        )
        proxy_stdout.close()
        proxy_stderr.close()

        with sync_playwright() as playwright:
            browser_home = temp_root / "browser-home"
            browser_home.mkdir(mode=0o700)
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=["--disable-dev-shm-usage", "--no-first-run"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 760}, ignore_https_errors=True
            )
            context.add_cookies(cookies)
            request_context = playwright.request.new_context(ignore_https_errors=True)
            wait_url(f"{frontend_origin}/_termdeck/healthz", request_context, 30)
            request_context.dispose()
            page = context.new_page()
            sockets: list[dict[str, Any]] = []

            def websocket(websocket_value: Any) -> None:
                record: dict[str, Any] = {
                    "url": websocket_value.url,
                    "received": 0,
                    "errors": [],
                }
                sockets.append(record)
                websocket_value.on(
                    "framereceived",
                    lambda _payload: record.__setitem__(
                        "received", int(record["received"]) + 1
                    ),
                )
                websocket_value.on(
                    "socketerror",
                    lambda error: record["errors"].append(str(error)),
                )

            page.on("websocket", websocket)
            page.goto(f"{frontend_origin}/random", wait_until="domcontentloaded", timeout=30_000)
            page.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=20_000)
            page.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=20_000,
            )
            page.locator("#terminal-xterm .xterm-screen").click(position={"x": 40, "y": 40})
            page.keyboard.type("printf 'HTTPS_WEBSOCKET_OK\\n'")
            page.keyboard.press("Enter")
            page.wait_for_function(
                r'''() => {
                  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
                  const buffer = terminal?.buffer?.active;
                  if (!buffer) return false;
                  for (let y=0; y<buffer.length; y+=1) {
                    if (buffer.getLine(y)?.translateToString(true).includes('HTTPS_WEBSOCKET_OK')) return true;
                  }
                  return false;
                }''',
                timeout=15_000,
            )
            if not sockets or not sockets[0]["url"].startswith("wss://"):
                raise RuntimeError(f"secure WebSocket was not observed: {sockets!r}")
            if any(record["errors"] for record in sockets):
                raise RuntimeError(f"secure WebSocket errors: {sockets!r}")
            screenshot = evidence / "https-lan.png"
            page.screenshot(path=str(screenshot), full_page=True)
            payload = {
                "schema": "termdeck.https-websocket-proof.v1",
                "ok": True,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "proxy": proxy_kind,
                "proxy_config_sha256": hashlib.sha256(
                    proxy_config.read_bytes()
                ).hexdigest(),
                "certificate_sha256": (
                    hashlib.sha256(certificate.read_bytes()).hexdigest()
                    if certificate.is_file() else None
                ),
                "lan_address": address,
                "frontend_port": frontend_port,
                "upstream_port": backend_port,
                "url": f"{frontend_origin}/random",
                "websockets": sockets,
                "screenshot_bytes": screenshot.stat().st_size,
            }
            (evidence / "result.json").write_text(
                json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
            )
            print(json.dumps(payload, sort_keys=True))
            context.close()
            browser.close()
            context = None
            browser = None
        return 0
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        terminate(proxy)
        terminate(backend)
        subprocess.run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
