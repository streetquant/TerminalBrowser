#!/usr/bin/env python3
"""Check upgraded-session revocation on an isolated tmux/server/auth fixture.

Raw browser sockets deliberately omit the app's HTTP polling: client-side
logout handling must not conceal a connection that the server still accepts.
Only the fixture is addressed; authentication secrets stay in memory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from check_web_terminal_e2e import (
    IsolatedServer, create_invitation, free_port, pair, tmux,
    write_registry, write_tmux_wrapper,
)


def poll(page, predicate: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Runtime evaluation avoids wait_for_function's string compilation,
        # which the production CSP correctly rejects without unsafe-eval.
        if page.evaluate(predicate):
            return
        time.sleep(.05)
    raise TimeoutError("socket condition deadline")


def open_socket(browser, origin: str, cookies):
    context = browser.new_context()
    context.add_cookies(cookies)
    page = context.new_page()
    page.goto(origin + "/_termdeck/healthz", wait_until="domcontentloaded")
    page.evaluate("""url => {
        window.probe = {ready:false, closed:false, pongs:0, error:null};
        window.socket = new WebSocket(url, 'termdeck.terminal.v1');
        socket.onopen = () => socket.send(JSON.stringify({type:'hello',cols:80,rows:24}));
        socket.onmessage = e => {
            if (typeof e.data !== 'string') return;
            const message = JSON.parse(e.data);
            if (message.type === 'ready') probe.ready = true;
            if (message.type === 'pong') probe.pongs++;
            if (message.type === 'error') probe.error = message.code;
        };
        socket.onclose = () => probe.closed = true;
    }""", origin.replace("http:", "ws:") + "/_termdeck/ws/v1/windows/random/panes/td-fixture")
    poll(page, "() => window.probe.ready")
    return context, page


def pong(page) -> None:
    count = page.evaluate("window.probe.pongs")
    page.evaluate("socket.send(JSON.stringify({type:'ping'}))")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.evaluate("window.probe.pongs") > count:
            return
        time.sleep(.05)
    raise TimeoutError("authenticated pong deadline")


def headers(origin, cookies):
    return {"Origin": origin, "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors", "Content-Type": "application/json",
            "Cookie": "; ".join(c["name"] + "=" + c["value"] for c in cookies),
            "X-TermDeck-CSRF": next(c["value"] for c in cookies if c["name"] == "termdeck_csrf")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"checks": {}, "cleanup": {}, "scope": "isolated native browser sockets; no production terminals"}
    socket_name = f"termdeck-socket-auth-{time.time_ns()}"
    server = None
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=evidence) as temporary:
        root = Path(temporary)
        database, auth_dir, wrapper = root / "sessions.sqlite3", root / "auth", root / "tmux"
        try:
            write_tmux_wrapper(wrapper, socket_name)
            tmux(socket_name, "-f", "/dev/null", "new-session", "-d", "-s", "termdeck_fixture_random", "/usr/bin/bash", "--noprofile", "--norc")
            write_registry(database, "termdeck_fixture_random", root)
            port = free_port()
            origin = f"http://127.0.0.1:{port}"
            command = [str(binary), "--db", str(database), "--listen", f"127.0.0.1:{port}",
                       "--auth-dir", str(auth_dir), "--host-id", "host-e2e", "--owner-id", "owner-e2e",
                       "--allow-insecure-loopback-auth", "--tmux", str(wrapper), "--session-ttl-ms", "120000"]
            admin_invitation = create_invitation(binary, auth_dir, "Disposable auth coordinator")
            server = IsolatedServer(command, root, origin, evidence)
            server.start()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
                try:
                    admin_cookies = pair(origin, admin_invitation)
                    admin_headers = headers(origin, admin_cookies)
                    api = pw.request.new_context()
                    observer_context, observer = open_socket(browser, origin, admin_cookies)
                    for action in ("logout", "device-revoke"):
                        result["phase"] = action
                        cookies = pair(origin, create_invitation(binary, auth_dir, "Disposable " + action))
                        context, page = open_socket(browser, origin, cookies)
                        pong(page)
                        result["checks"][action + "_authenticated_socket"] = True
                        user_headers = headers(origin, cookies)
                        session = api.get(origin + "/_termdeck/api/v1/session", headers=user_headers).json()
                        if action == "logout":
                            response = api.post(origin + "/_termdeck/api/v1/logout", data={}, headers=user_headers)
                        else:
                            response = api.post(origin + "/_termdeck/api/v1/devices/" + session["device_id"] + "/revoke", data={}, headers=admin_headers)
                        assert response.status == 200
                        denied = api.get(origin + "/_termdeck/api/v1/session", headers=user_headers)
                        result["checks"][action + "_stale_cookie_denied"] = denied.status == 401
                        started = time.monotonic()
                        poll(page, "() => window.probe.closed", timeout=5)
                        result["checks"][action + "_idle_socket_closed"] = True
                        result[action + "_close_ms"] = round((time.monotonic() - started) * 1000, 1)
                        result["checks"][action + "_redacted_error"] = page.evaluate("window.probe.error") == "authentication-required"
                        pong(observer)
                        result["checks"][action + "_other_session_unchanged"] = True
                        context.close()
                    observer_context.close()
                    api.dispose()
                    # A fresh short-lived session covers expiry without editing
                    # an authentication database or advancing the host's clock.
                    server.stop()
                    result["phase"] = "expiry"
                    command[-1] = "2000"
                    server = IsolatedServer(command, root, origin, evidence / "expiry")
                    (evidence / "expiry").mkdir(mode=0o700)
                    server.start()
                    cookies = pair(origin, create_invitation(binary, auth_dir, "Disposable expiry"))
                    context, page = open_socket(browser, origin, cookies)
                    pong(page)
                    result["checks"]["expiry_authenticated_socket"] = True
                    poll(page, "() => window.probe.closed", timeout=5)
                    result["checks"]["expiry_idle_socket_closed"] = True
                    result["checks"]["expiry_redacted_error"] = page.evaluate("window.probe.error") == "authentication-required"
                    context.close()
                finally:
                    browser.close()
        except Exception as error:
            # HTTP exception text can include cookie traces; retain type only.
            result["failure"] = type(error).__name__
        finally:
            if server:
                server.stop()
                result["cleanup"]["own_server_stopped"] = server.process is None
            stopped = subprocess.run(["/usr/bin/tmux", "-L", socket_name, "kill-server"], capture_output=True, timeout=10)
            result["cleanup"]["own_tmux_stopped"] = stopped.returncode == 0
    result["cleanup"]["own_auth_and_registry_removed"] = not root.exists()
    passed = not result.get("failure") and bool(result["checks"]) and all(result["checks"].values()) and all(result["cleanup"].values())
    result["ok"] = passed
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
