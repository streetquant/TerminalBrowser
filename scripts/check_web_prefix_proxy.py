#!/usr/bin/env python3
"""Disposable HTTPS/WSS proof for TermDeck behind the /t1000 Cloudflare prefix.

The gate uses a private tmux socket, registry, authentication store, nginx instance,
TLS keypair and Chromium profile. It cannot address production terminal panes.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HOSTNAME = "maxchat.semiotech.org"
PREFIX = "/t1000"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def wait_until(predicate: Callable[[], bool], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except BaseException as exc:  # noqa: BLE001 - retained for bounded diagnostics
            last = exc
        time.sleep(0.05)
    suffix = f": {last}" if last else ""
    raise RuntimeError(f"timed out waiting for {detail}{suffix}")


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=8)


def write_registry(path: Path, tmux_name: str) -> None:
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
            INSERT INTO remembered_windows VALUES ('win-prefix', 'Random');
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "td-prefix",
                "Prefix fixture",
                str(ROOT),
                tmux_name,
                "running",
                "win-prefix",
                "Random",
                0,
                1,
                0,
                "2026-08-30T00:00:00Z",
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
            "host-prefix",
            "--owner-id",
            "owner-prefix",
            "--create-invitation",
            "Prefixed Chromium",
        ],
        cwd=ROOT,
        text=True,
        timeout=30,
    )
    return str(json.loads(output)["invitation_secret"])


def pair_backend(origin: str, secret: str) -> list[dict[str, Any]]:
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
    cookies = [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": HOSTNAME,
            "path": "/",
            "httpOnly": cookie.name.endswith("session"),
            "secure": True,
            "sameSite": "Strict",
        }
        for cookie in jar
    ]
    if {cookie["name"] for cookie in cookies} != {"termdeck_session", "termdeck_csrf"}:
        raise RuntimeError("pairing did not issue both TermDeck cookies")
    return cookies


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    nginx = shutil.which("nginx")
    openssl = shutil.which("openssl")
    if nginx is None or openssl is None:
        raise RuntimeError("nginx and openssl are required")

    temp_parent = ROOT / ".ai-bridge" / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="termdeck-prefix-proxy-", dir=temp_parent))
    socket_name = f"termdeck-prefix-{os.getpid()}-{time.time_ns()}"
    tmux_name = "termdeck_prefix_random"
    backend_port = free_port()
    frontend_port = free_port()
    backend_origin = f"http://127.0.0.1:{backend_port}"
    external_origin = f"https://{HOSTNAME}:{frontend_port}"
    database = temp / "sessions.sqlite3"
    auth_parent = temp / "auth-parent"
    auth_parent.mkdir(mode=0o700)
    auth_dir = auth_parent / "auth"
    tmux_wrapper = temp / "tmux"
    t1000_wrapper = temp / "t1000"
    certificate = temp / "server.crt"
    private_key = temp / "server.key"
    nginx_home = temp / "nginx"
    nginx_home.mkdir(mode=0o700)
    for name in ("client", "proxy"):
        (nginx_home / name).mkdir(mode=0o700)
    config = temp / "nginx.conf"
    backend: subprocess.Popen[Any] | None = None
    proxy: subprocess.Popen[Any] | None = None
    browser: Any = None
    contexts: list[Any] = []
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
                "120",
                "-y",
                "36",
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
            ],
            check=True,
            timeout=10,
        )
        write_registry(database, tmux_name)
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
                "host-prefix",
                "--owner-id",
                "owner-prefix",
                "--auth-dir",
                str(auth_dir),
                "--allow-insecure-loopback-auth",
            ],
            cwd=ROOT,
            stdout=backend_stdout,
            stderr=backend_stderr,
            start_new_session=True,
        )
        backend_stdout.close()
        backend_stderr.close()
        wait_until(
            lambda: urllib.request.urlopen(
                f"{backend_origin}/_termdeck/healthz", timeout=1
            ).status
            == 200,
            timeout=20,
            detail="backend health",
        )
        cookies = pair_backend(backend_origin, secret)
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
                f"/CN={HOSTNAME}",
                "-addext",
                f"subjectAltName=DNS:{HOSTNAME}",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        private_key.chmod(0o600)
        config.write_text(
            f"""worker_processes 1;
pid {nginx_home}/nginx.pid;
error_log stderr info;
events {{ worker_connections 512; }}
http {{
  access_log off;
  client_body_temp_path {nginx_home}/client;
  proxy_temp_path {nginx_home}/proxy;
  map $http_upgrade $connection_upgrade {{ default upgrade; '' ''; }}
  server {{
    listen 127.0.0.1:{frontend_port} ssl;
    server_name {HOSTNAME};
    ssl_certificate {certificate};
    ssl_certificate_key {private_key};
    location = {PREFIX} {{ return 308 {PREFIX}/; }}
    location ^~ {PREFIX}/ {{
      rewrite ^{PREFIX}(/.*)$ $1 break;
      proxy_pass http://127.0.0.1:{backend_port};
      proxy_http_version 1.1;
      proxy_set_header Host 127.0.0.1:{backend_port};
      proxy_set_header Origin http://127.0.0.1:{backend_port};
      proxy_set_header Sec-Fetch-Site same-origin;
      proxy_set_header Sec-Fetch-Mode $http_sec_fetch_mode;
      proxy_set_header X-Forwarded-Prefix {PREFIX};
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Host {HOSTNAME};
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_read_timeout 1h;
      proxy_send_timeout 1h;
    }}
    location / {{ return 404; }}
  }}
}}
""",
            encoding="utf-8",
        )
        subprocess.run(
            [nginx, "-p", f"{nginx_home}/", "-c", str(config), "-t"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        proxy_stdout = (evidence / "nginx.stdout").open("wb")
        proxy_stderr = (evidence / "nginx.stderr").open("wb")
        proxy = subprocess.Popen(
            [nginx, "-p", f"{nginx_home}/", "-c", str(config), "-g", "daemon off;"],
            stdout=proxy_stdout,
            stderr=proxy_stderr,
            start_new_session=True,
        )
        proxy_stdout.close()
        proxy_stderr.close()
        wait_until(
            lambda: can_connect("127.0.0.1", frontend_port),
            timeout=20,
            detail="prefix proxy listener",
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    f"--host-resolver-rules=MAP {HOSTNAME} 127.0.0.1",
                ],
            )
            unauth = browser.new_context(ignore_https_errors=True)
            contexts.append(unauth)
            unauth_page = unauth.new_page()
            unauth_page.goto(
                f"{external_origin}{PREFIX}/random",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            unauth_path = urllib.parse.urlparse(unauth_page.url).path
            if unauth_path not in {
                f"{PREFIX}/_termdeck/login",
                f"{PREFIX}/_termdeck/setup",
            }:
                raise RuntimeError(f"prefix authentication redirect escaped: {unauth_page.url}")

            context = browser.new_context(
                viewport={"width": 1440, "height": 900}, ignore_https_errors=True
            )
            contexts.append(context)
            context.add_cookies(cookies)
            page = context.new_page()
            requests: list[str] = []
            failed_requests: list[str] = []
            console_messages: list[dict[str, str]] = []
            page_errors: list[str] = []
            sockets: list[dict[str, Any]] = []
            page.on("request", lambda request: requests.append(request.url))
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(
                    f"{request.method} {request.url} {request.failure}"
                ),
            )
            page.on(
                "console",
                lambda message: console_messages.append(
                    {"type": message.type, "text": message.text[:2_000]}
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)[:4_000]))

            def websocket(value: Any) -> None:
                record: dict[str, Any] = {
                    "url": value.url,
                    "received": 0,
                    "errors": [],
                    "closed": False,
                }
                sockets.append(record)
                value.on(
                    "framereceived",
                    lambda _payload: record.__setitem__("received", int(record["received"]) + 1),
                )
                value.on("socketerror", lambda error: record["errors"].append(str(error)))
                value.on("close", lambda: record.__setitem__("closed", True))

            page.on("websocket", websocket)
            started = time.monotonic()
            page.goto(
                f"{external_origin}{PREFIX}/random",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            page.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=20_000)
            page.wait_for_function(
                "() => document.querySelector('#terminal-status')?.dataset.state === 'live'",
                timeout=20_000,
            )
            live_ms = (time.monotonic() - started) * 1000
            if page.locator("#termdeck-app").get_attribute("data-termdeck-base") != PREFIX:
                raise RuntimeError("browser did not receive the external base path")
            page.locator("#terminal-xterm .xterm-screen").click(position={"x": 48, "y": 48})
            marker = "PREFIX_WSS_INPUT_OK"
            page.keyboard.type(f"printf '{marker}\\n'")
            page.keyboard.press("Enter")
            wait_until(
                lambda: marker
                in subprocess.check_output(
                    [
                        "/usr/bin/tmux",
                        "-L",
                        socket_name,
                        "capture-pane",
                        "-epq",
                        "-t",
                        f"{tmux_name}:0.0",
                        "-S",
                        "-200",
                    ],
                    text=True,
                    timeout=5,
                ),
                timeout=15,
                detail="prefixed WSS keyboard marker",
            )
            page.goto(
                f"{external_origin}{PREFIX}/random/t/td-prefix",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            page.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=20_000)
            try:
                page.wait_for_function(
                    "() => ['live', 'viewer'].includes(document.querySelector('#terminal-status')?.dataset.state)",
                    timeout=20_000,
                )
            except Exception:
                diagnostic = {
                    "url": page.url,
                    "title": page.title(),
                    "status": page.locator("#terminal-status").inner_text()
                    if page.locator("#terminal-status").count()
                    else None,
                    "status_state": page.locator("#terminal-status").get_attribute("data-state")
                    if page.locator("#terminal-status").count()
                    else None,
                    "body_connection": page.locator("body").get_attribute("data-termdeck-connection"),
                    "base_path": page.locator("#termdeck-app").get_attribute("data-termdeck-base")
                    if page.locator("#termdeck-app").count()
                    else None,
                    "sockets": sockets,
                    "failed_requests": failed_requests,
                    "console": console_messages,
                    "page_errors": page_errors,
                    "requests_tail": requests[-80:],
                    "body_text": page.locator("body").inner_text()[-8_000:],
                }
                (evidence / "direct-route-failure.json").write_text(
                    json.dumps(diagnostic, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                page.screenshot(path=str(evidence / "direct-route-failure.png"), full_page=True)
                raise
            if not sockets or any(not item["url"].startswith(f"wss://{HOSTNAME}:{frontend_port}{PREFIX}/_termdeck/ws/") for item in sockets):
                raise RuntimeError(f"prefixed secure WebSocket missing: {sockets!r}")
            if any(item["errors"] for item in sockets):
                raise RuntimeError(f"prefixed secure WebSocket errors: {sockets!r}")
            escaped = [
                url
                for url in requests
                if urllib.parse.urlparse(url).netloc == f"{HOSTNAME}:{frontend_port}"
                and not urllib.parse.urlparse(url).path.startswith(PREFIX)
            ]
            if escaped:
                raise RuntimeError(f"application requests escaped the prefix: {escaped[:20]!r}")
            screenshot = evidence / "prefix-workspace.png"
            page.screenshot(path=str(screenshot), full_page=True)
            payload = {
                "schema": "termdeck.prefixed-https-wss.v1",
                "ok": True,
                "binary_sha256": sha256(binary),
                "nginx_config_sha256": sha256(config),
                "origin": external_origin,
                "prefix": PREFIX,
                "live_ms": round(live_ms, 3),
                "websockets": sockets,
                "request_count": len(requests),
                "escaped_requests": escaped,
                "screenshot_sha256": sha256(screenshot),
                "screenshot_bytes": screenshot.stat().st_size,
            }
            (evidence / "result.json").write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(payload, sort_keys=True))
        return 0
    finally:
        for context in reversed(contexts):
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        terminate(proxy)
        terminate(backend)
        subprocess.run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
