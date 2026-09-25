#!/usr/bin/env python3
"""Disposable Chromium proof for TermDeck's full browser terminal.

The gate creates a private tmux socket, temporary registry/auth databases, random
loopback ports, isolated server processes, and independent Chromium contexts.
It never addresses the default tmux server or the production TermDeck registry.
"""

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
import textwrap
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

REJECTED_UI = (
    "Take control",
    "Take over control",
    "Release control",
    "Send text",
    "Controller active",
    "typing switches control here",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001 - bounded readiness polling
            last_error = error
        time.sleep(0.05)
    raise RuntimeError(f"HTTP readiness timed out for {url}: {last_error}")


def run_checked(argv: list[str], *, timeout: float = 20.0, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(argv, check=True, capture_output=True, text=text, timeout=timeout)


def tmux(socket_name: str, *arguments: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return run_checked(["/usr/bin/tmux", "-L", socket_name, *arguments], timeout=timeout)


def tmux_capture(socket_name: str, target: str) -> str:
    return tmux(socket_name, "capture-pane", "-epq", "-t", target, "-S", "-4000").stdout


def write_registry(path: Path, tmux_name: str, cwd: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              cwd TEXT NOT NULL,
              tmux_name TEXT NOT NULL,
              status TEXT NOT NULL,
              window_id TEXT NOT NULL,
              window_title TEXT NOT NULL,
              tab_index INTEGER NOT NULL,
              visible INTEGER NOT NULL,
              pinned INTEGER NOT NULL,
              last_opened_at TEXT NOT NULL,
              pane_target TEXT
            );
            CREATE TABLE remembered_windows (
              window_id TEXT PRIMARY KEY,
              window_title TEXT NOT NULL
            );
            INSERT INTO remembered_windows VALUES ('win-fixture', 'Random');
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "td-fixture",
                "Codex fixture",
                str(cwd),
                tmux_name,
                "running",
                "win-fixture",
                "Random",
                0,
                1,
                1,
                "2026-08-28T00:00:00Z",
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def registry_row(path: Path, session_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def write_fixture_programs(root: Path) -> tuple[Path, Path, Path]:
    alt_probe = root / "alt_probe.py"
    alt_probe.write_text(
        textwrap.dedent(
            r'''
            import sys
            sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[31mANSI_COLOR_OK\x1b[0m UNICODE=東京🙂 COMBINING=é ALT_SCREEN_OK\r\n")
            sys.stdout.write("Press Enter to return to the primary screen.\r\n")
            sys.stdout.flush()
            sys.stdin.readline()
            sys.stdout.write("\x1b[?1049lPRIMARY_AFTER_ALT\n")
            sys.stdout.flush()
            ''',
        ).lstrip(),
        encoding="utf-8",
    )

    key_probe = root / "key_probe.py"
    key_probe.write_text(
        textwrap.dedent(
            r'''
            import os, select, sys, termios, time, tty
            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)
            data = bytearray()
            try:
                tty.setraw(fd)
                os.write(sys.stdout.fileno(), b"KEY_PROBE_READY\r\n")
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and len(data) < 32:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                    if ready:
                        data.extend(os.read(fd, 64))
                    if data.count(0x1B) >= 6 and len(data) >= 18:
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)
            print("\r\nKEY_HEX=" + data.hex(), flush=True)
            ''',
        ).lstrip(),
        encoding="utf-8",
    )

    mouse_probe = root / "mouse_probe.py"
    mouse_probe.write_text(
        textwrap.dedent(
            r'''
            import os, select, sys, termios, time, tty
            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)
            data = bytearray()
            try:
                tty.setraw(fd)
                os.write(sys.stdout.fileno(), b"\x1b[?1000h\x1b[?1006hMOUSE_PROBE_READY\r\n")
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and len(data) < 128:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                    if ready:
                        data.extend(os.read(fd, 128))
                    if data.startswith(b"\x1b[<") and (data.endswith(b"M") or data.endswith(b"m")):
                        break
            finally:
                os.write(sys.stdout.fileno(), b"\x1b[?1000l\x1b[?1006l")
                termios.tcsetattr(fd, termios.TCSADRAIN, original)
            print("\r\nMOUSE_HEX=" + data.hex(), flush=True)
            ''',
        ).lstrip(),
        encoding="utf-8",
    )
    return alt_probe, key_probe, mouse_probe


def write_tmux_wrapper(path: Path, socket_name: str) -> None:
    path.write_text(f'#!/bin/sh\nexec /usr/bin/tmux -L {socket_name!s} "$@"\n', encoding="utf-8")
    path.chmod(0o700)


def write_t1000_wrapper(path: Path, socket_name: str) -> None:
    script = f'''#!/usr/bin/python3
from __future__ import annotations
import json, sqlite3, subprocess, sys, time, uuid

SOCKET = {socket_name!r}
args = sys.argv[1:]
if not args:
    raise SystemExit(2)
action = args.pop(0)

def take(flag, default=None):
    if flag not in args:
        return default
    index = args.index(flag)
    if index + 1 >= len(args):
        raise SystemExit(2)
    value = args[index + 1]
    del args[index:index + 2]
    return value

def remove(flag):
    while flag in args:
        args.remove(flag)

path = take('--db')
if not path:
    raise SystemExit(2)
remove('--yes')
remove('--include-pinned')
connection = sqlite3.connect(path, timeout=10)
try:
    if action == 'new':
        cwd = take('--cwd')
        window_id = take('--window-id')
        window_title = take('--window-title')
        name = take('--name', 'New tab')
        command = take('--command')
        pinned = 1 if '--pinned' in args else 0
        remove('--pinned')
        if args:
            raise SystemExit(2)
        session_id = 'td-e2e-' + uuid.uuid4().hex[:12]
        tmux_name = 'termdeck_' + session_id.replace('-', '_')
        subprocess.run(['/usr/bin/tmux', '-L', SOCKET, '-f', '/dev/null', 'new-session', '-d', '-s', tmux_name, '-x', '100', '-y', '30', '/usr/bin/bash', '--noprofile', '--norc'], check=True)
        if command:
            subprocess.run(['/usr/bin/tmux', '-L', SOCKET, 'send-keys', '-t', tmux_name + ':0.0', '-l', command], check=True)
            subprocess.run(['/usr/bin/tmux', '-L', SOCKET, 'send-keys', '-t', tmux_name + ':0.0', 'Enter'], check=True)
        next_index = connection.execute('SELECT COALESCE(MAX(tab_index), -1) + 1 FROM sessions WHERE window_id = ?', (window_id,)).fetchone()[0]
        connection.execute(
            'INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (session_id, name, cwd, tmux_name, 'running', window_id, window_title, next_index, 1, pinned, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), None),
        )
        connection.commit()
        payload = {{'ok': True, 'tabs': [{{'id': session_id, 'name': name, 'cwd': cwd}}]}}
    else:
        if action == 'rename':
            if len(args) != 2:
                raise SystemExit(2)
            session_id, name = args
            connection.execute('UPDATE sessions SET name = ? WHERE id = ?', (name, session_id))
        else:
            if len(args) != 1:
                raise SystemExit(2)
            session_id = args[0]
            row = connection.execute('SELECT tmux_name FROM sessions WHERE id = ?', (session_id,)).fetchone()
            if row is None:
                raise SystemExit(3)
            if action == 'pause':
                connection.execute("UPDATE sessions SET status = 'paused' WHERE id = ?", (session_id,))
            elif action == 'resume':
                connection.execute("UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,))
            elif action == 'pin':
                connection.execute('UPDATE sessions SET pinned = 1 WHERE id = ?', (session_id,))
            elif action == 'unpin':
                connection.execute('UPDATE sessions SET pinned = 0 WHERE id = ?', (session_id,))
            elif action == 'hide':
                connection.execute('UPDATE sessions SET visible = 0 WHERE id = ?', (session_id,))
            elif action == 'kill':
                subprocess.run(['/usr/bin/tmux', '-L', SOCKET, 'kill-session', '-t', row[0]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                connection.execute("UPDATE sessions SET status = 'stopped', visible = 0 WHERE id = ?", (session_id,))
            else:
                raise SystemExit(2)
        connection.commit()
        payload = {{'ok': True, 'action': action, 'id': session_id}}
    print(json.dumps(payload, sort_keys=True))
finally:
    connection.close()
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)


def create_invitation(binary: Path, auth_dir: Path, label: str) -> str:
    output = run_checked(
        [
            str(binary),
            "--auth-dir",
            str(auth_dir),
            "--host-id",
            "host-e2e",
            "--owner-id",
            "owner-e2e",
            "--create-invitation",
            label,
        ],
        timeout=30,
    ).stdout
    payload = json.loads(output)
    secret = payload.get("invitation_secret")
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("invitation secret was absent")
    return secret


def pair(origin: str, invitation: str) -> list[dict[str, Any]]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        f"{origin}/_termdeck/pair",
        data=urllib.parse.urlencode({"invitation": invitation}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    )
    with opener.open(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"pairing failed with HTTP {response.status}")
    cookies = [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": "127.0.0.1",
            "path": cookie.path or "/",
            "httpOnly": cookie.name.endswith("session"),
            "secure": False,
            "sameSite": "Strict",
        }
        for cookie in jar
    ]
    if {cookie["name"] for cookie in cookies} != {"termdeck_session", "termdeck_csrf"}:
        raise RuntimeError("pairing did not issue the expected cookies")
    return cookies


class IsolatedServer:
    def __init__(self, command: list[str], root: Path, origin: str, evidence: Path) -> None:
        self.command = command
        self.root = root
        self.origin = origin
        self.evidence = evidence
        self.process: subprocess.Popen[bytes] | None = None
        self.generation = 0

    def start(self) -> float:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("isolated server is already running")
        self.generation += 1
        stdout = (self.evidence / f"server-{self.generation}.stdout").open("wb")
        stderr = (self.evidence / f"server-{self.generation}.stderr").open("wb")
        started = time.monotonic()
        self.process = subprocess.Popen(
            self.command,
            cwd=self.root,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        stdout.close()
        stderr.close()
        wait_http(f"{self.origin}/_termdeck/healthz")
        return (time.monotonic() - started) * 1000.0

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def new_context(playwright: Playwright, cookies: list[dict[str, Any]], viewport: dict[str, int]) -> tuple[Any, BrowserContext, Page]:
    browser = playwright.chromium.launch(
        executable_path="/usr/bin/chromium",
        headless=True,
        args=(
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ),
    )
    context = browser.new_context(viewport=viewport)
    context.add_cookies(cookies)
    page = context.new_page()
    return browser, context, page


def buffer_script() -> str:
    return r'''() => {
      const api = document.querySelector('#terminal-xterm')?.termdeckTerminal;
      if (!api?.terminal?.buffer?.active) return '';
      const buffer = api.terminal.buffer.active;
      const lines = [];
      for (let y = 0; y < buffer.length; y += 1) {
        const line = buffer.getLine(y);
        if (line) lines.push(line.translateToString(true));
      }
      return lines.join('\n');
    }'''


def buffer_text(page: Page) -> str:
    value = page.evaluate(buffer_script())
    return value if isinstance(value, str) else ""


def wait_buffer(page: Page, marker: str, timeout: float = 15_000) -> None:
    page.wait_for_function(
        r'''marker => {
          const api = document.querySelector('#terminal-xterm')?.termdeckTerminal;
          if (!api?.terminal?.buffer?.active) return false;
          const buffer = api.terminal.buffer.active;
          for (let y = 0; y < buffer.length; y += 1) {
            if (buffer.getLine(y)?.translateToString(true).includes(marker)) return true;
          }
          return false;
        }''',
        arg=marker,
        timeout=timeout,
    )


def focus_terminal(page: Page) -> None:
    page.locator("#terminal-xterm .xterm-screen").click(position={"x": 40, "y": 40})
    page.locator("#terminal-xterm .xterm-helper-textarea").focus()


def type_command(page: Page, command: str, marker: str | None = None, timeout: float = 15_000) -> None:
    focus_terminal(page)
    page.keyboard.type(command)
    page.keyboard.press("Enter")
    if marker:
        wait_buffer(page, marker, timeout)


def control_state(page: Page, origin: str, pane_id: str) -> dict[str, Any]:
    value = page.evaluate(
        """async ({url}) => {
          const response = await fetch(url, {cache:'no-store', credentials:'same-origin'});
          return {status: response.status, payload: await response.json()};
        }""",
        {"url": f"{origin}/_termdeck/api/v1/windows/random/panes/{pane_id}/control"},
    )
    if not isinstance(value, dict) or value.get("status") != 200:
        raise RuntimeError(f"controller status request failed: {value!r}")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError("controller status payload was invalid")
    nested = payload.get("control")
    return nested if isinstance(nested, dict) else payload


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:  # noqa: BLE001 - bounded polling
            last_error = error
        time.sleep(interval)
    raise RuntimeError(f"condition timed out: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--max-live-ms", type=float, default=10_000.0)
    arguments = parser.parse_args()

    root = Path.cwd().resolve()
    binary = arguments.binary.resolve()
    evidence = arguments.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if not binary.is_file():
        raise RuntimeError(f"server binary does not exist: {binary}")

    temp_parent = root / ".ai-bridge" / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="termdeck-terminal-e2e-", dir=temp_parent))
    browser_home = run_root / "browser-home"
    browser_home.mkdir(mode=0o700)
    (browser_home / ".config").mkdir(mode=0o700)
    old_home = os.environ.get("HOME")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    old_tmp = os.environ.get("TMPDIR")
    os.environ["HOME"] = str(browser_home)
    os.environ["XDG_CONFIG_HOME"] = str(browser_home / ".config")
    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = None

    socket_name = f"termdeck-e2e-{os.getpid()}-{time.time_ns()}"
    tmux_name = "termdeck_fixture_random"
    tmux_target = f"{tmux_name}:0.0"
    database = run_root / "sessions.sqlite3"
    auth_parent = run_root / "auth-parent"
    auth_parent.mkdir(mode=0o700)
    auth_dir = auth_parent / "auth"
    tmux_wrapper = run_root / "tmux"
    t1000_wrapper = run_root / "t1000"
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    console: list[dict[str, str]] = []
    page_errors: list[str] = []
    request_failures: list[str] = []
    websocket_records: list[dict[str, Any]] = []
    result: dict[str, Any] = {"schema": "termdeck.browser-terminal-e2e.v1"}
    server: IsolatedServer | None = None
    browsers: list[Any] = []
    contexts: list[BrowserContext] = []

    try:
        alt_probe, key_probe, mouse_probe = write_fixture_programs(run_root)
        write_tmux_wrapper(tmux_wrapper, socket_name)
        write_t1000_wrapper(t1000_wrapper, socket_name)
        tmux(
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
        )
        tmux(socket_name, "send-keys", "-t", tmux_target, "-l", f"/usr/bin/python3 {alt_probe}")
        tmux(socket_name, "send-keys", "-t", tmux_target, "Enter")
        time.sleep(0.25)
        alternate = tmux(socket_name, "display-message", "-p", "-t", tmux_target, "#{alternate_on}").stdout.strip()
        if alternate != "1":
            raise RuntimeError("fixture did not enter the alternate screen")
        write_registry(database, tmux_name, root)

        first_invitation = create_invitation(binary, auth_dir, "Disposable Chromium A")
        second_invitation = create_invitation(binary, auth_dir, "Disposable Chromium B")
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
            "--tmux",
            str(tmux_wrapper),
            "--tmux-timeout-ms",
            "3000",
            "--t1000",
            str(t1000_wrapper),
            "--tab-action-timeout-ms",
            "10000",
            "--host-id",
            "host-e2e",
            "--owner-id",
            "owner-e2e",
            "--auth-dir",
            str(auth_dir),
            "--allow-insecure-loopback-auth",
        ]
        server = IsolatedServer(command, root, origin, evidence)
        result["server_start_ms"] = round(server.start(), 3)
        cookies_one = pair(origin, first_invitation)
        cookies_two = pair(origin, second_invitation)

        with urllib.request.urlopen(f"{origin}/_termdeck/assets/v7/terminal.js", timeout=5) as response:
            result["terminal_asset_cache_control"] = response.headers.get("Cache-Control")
        opener = urllib.request.build_opener()
        request = urllib.request.Request(f"{origin}/_termdeck/healthz")
        with opener.open(request, timeout=5) as response:
            result["page_cache_control"] = response.headers.get("Cache-Control")
            result["content_security_policy"] = response.headers.get("Content-Security-Policy")

        with sync_playwright() as playwright:
            browser_one, context_one, page_one = new_context(
                playwright, cookies_one, {"width": 1440, "height": 900}
            )
            browsers.append(browser_one)
            contexts.append(context_one)
            page_one.on(
                "console",
                lambda message: console.append({"type": message.type, "text": message.text[:2000]}),
            )
            page_one.on("pageerror", lambda error: page_errors.append(str(error)[:4000]))
            page_one.on(
                "requestfailed",
                lambda request: request_failures.append(
                    f"{request.method} {request.url} {request.failure}"
                ),
            )

            def websocket_handler(websocket: Any) -> None:
                record: dict[str, Any] = {
                    "url": websocket.url,
                    "sent": 0,
                    "received": 0,
                    "errors": [],
                }
                websocket_records.append(record)
                websocket.on(
                    "framesent",
                    lambda _payload: record.__setitem__("sent", int(record["sent"]) + 1),
                )
                websocket.on(
                    "framereceived",
                    lambda _payload: record.__setitem__(
                        "received", int(record["received"]) + 1
                    ),
                )
                websocket.on(
                    "socketerror",
                    lambda error: record["errors"].append(str(error)[:1000]),
                )

            page_one.on("websocket", websocket_handler)
            started = time.monotonic()
            page_one.goto(f"{origin}/random", wait_until="domcontentloaded", timeout=15_000)
            page_one.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=15_000)
            result["xterm_visible_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            page_one.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=15_000,
            )
            result["terminal_live_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            wait_buffer(page_one, "ALT_SCREEN_OK", 15_000)
            result["alternate_replay_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            if result["terminal_live_ms"] > arguments.max_live_ms:
                raise RuntimeError(
                    f"terminal became live in {result['terminal_live_ms']} ms, above {arguments.max_live_ms} ms"
                )
            first_html = page_one.content()
            for rejected in REJECTED_UI:
                if rejected in first_html:
                    raise RuntimeError(f"rejected operator UI remains: {rejected}")
            if page_one.get_by_role("button", name="New tab", exact=True).count() != 1:
                raise RuntimeError("browser New Tab action is absent")
            color_mode = page_one.evaluate(
                r'''() => {
                  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
                  const buffer = terminal?.buffer?.active;
                  if (!buffer) return -1;
                  for (let y = 0; y < buffer.length; y += 1) {
                    const line = buffer.getLine(y);
                    const text = line?.translateToString(true) || '';
                    const index = text.indexOf('ANSI_COLOR_OK');
                    if (index >= 0) {
                      const cell = line.getCell(index);
                      if (!cell) return -2;
                      return typeof cell.getFgColorMode === 'function' ? cell.getFgColorMode() : (cell.isFgDefault?.() ? 0 : 1);
                    }
                  }
                  return -3;
                }'''
            )
            if not isinstance(color_mode, int) or color_mode <= 0:
                raise RuntimeError(f"ANSI foreground state was not preserved: {color_mode!r}")
            result["ansi_color_mode"] = color_mode
            initial_buffer = buffer_text(page_one)
            if "東京🙂" not in initial_buffer or "COMBINING=é" not in initial_buffer:
                raise RuntimeError("Unicode or combining-character replay failed")

            focus_terminal(page_one)
            page_one.keyboard.press("Enter")
            wait_buffer(page_one, "PRIMARY_AFTER_ALT", 10_000)
            type_command(page_one, "printf 'KEYBOARD_OK\\n'", "KEYBOARD_OK")

            focus_terminal(page_one)
            page_one.evaluate(
                r'''text => {
                  const textarea = document.querySelector('#terminal-xterm .xterm-helper-textarea');
                  const transfer = new DataTransfer();
                  transfer.setData('text/plain', text);
                  textarea.dispatchEvent(new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: transfer
                  }));
                }''',
                "printf 'PASTE_OK\\n'\n",
            )
            wait_buffer(page_one, "PASTE_OK", 10_000)

            focus_terminal(page_one)
            page_one.keyboard.type("sleep 30")
            page_one.keyboard.press("Enter")
            time.sleep(0.3)
            page_one.keyboard.press("Control+C")
            type_command(page_one, "printf 'CTRL_C_OK\\n'", "CTRL_C_OK")

            type_command(page_one, "cat")
            focus_terminal(page_one)
            page_one.keyboard.press("Control+D")
            type_command(page_one, "printf 'CTRL_D_OK\\n'", "CTRL_D_OK")
            focus_terminal(page_one)
            page_one.keyboard.press("Control+L")
            type_command(page_one, "printf 'CTRL_L_OK\\n'", "CTRL_L_OK")

            try:
                type_command(page_one, f"/usr/bin/python3 {key_probe}", "KEY_PROBE_READY", 30_000)
            except Exception:
                diagnostic = {
                    "browser_buffer": buffer_text(page_one),
                    "tmux_capture": tmux_capture(socket_name, tmux_target),
                    "control": control_state(page_one, origin, "td-fixture"),
                }
                (evidence / "key-probe-diagnostic.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                raise
            focus_terminal(page_one)
            for key in ("Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
                page_one.keyboard.press(key)
            wait_buffer(page_one, "KEY_HEX=", 10_000)
            key_output = buffer_text(page_one)
            key_hex = key_output.rsplit("KEY_HEX=", 1)[-1].splitlines()[0].strip()
            key_bytes = bytes.fromhex(key_hex)
            if not key_bytes.startswith(b"\t\x1b"):
                raise RuntimeError(f"Tab/Escape input mismatch: {key_hex}")
            arrow_variants = (
                (b"\x1b[A", b"\x1bOA"),
                (b"\x1b[B", b"\x1bOB"),
                (b"\x1b[D", b"\x1bOD"),
                (b"\x1b[C", b"\x1bOC"),
            )
            if any(not any(sequence in key_bytes for sequence in variants) for variants in arrow_variants):
                raise RuntimeError(f"arrow key input mismatch: {key_hex}")
            result["key_hex"] = key_hex

            type_command(page_one, f"/usr/bin/python3 {mouse_probe}", "MOUSE_PROBE_READY")
            page_one.locator("#terminal-xterm .xterm-screen").click(position={"x": 200, "y": 120})
            wait_buffer(page_one, "MOUSE_HEX=", 10_000)
            mouse_output = buffer_text(page_one)
            mouse_hex = mouse_output.rsplit("MOUSE_HEX=", 1)[-1].splitlines()[0].strip()
            mouse_bytes = bytes.fromhex(mouse_hex)
            if not mouse_bytes.startswith(b"\x1b[<") or mouse_bytes[-1:] not in (b"M", b"m"):
                raise RuntimeError(f"SGR mouse input mismatch: {mouse_hex}")
            result["mouse_hex"] = mouse_hex

            before_size = tmux(
                socket_name,
                "display-message",
                "-p",
                "-t",
                tmux_target,
                "#{pane_width}x#{pane_height}",
            ).stdout.strip()
            page_one.set_viewport_size({"width": 1040, "height": 640})
            page_one.wait_for_timeout(500)
            compact_size = tmux(
                socket_name,
                "display-message",
                "-p",
                "-t",
                tmux_target,
                "#{pane_width}x#{pane_height}",
            ).stdout.strip()
            page_one.set_viewport_size({"width": 1680, "height": 1050})
            page_one.wait_for_timeout(500)
            after_size = tmux(
                socket_name,
                "display-message",
                "-p",
                "-t",
                tmux_target,
                "#{pane_width}x#{pane_height}",
            ).stdout.strip()
            if compact_size != before_size or after_size != before_size:
                raise RuntimeError(
                    f"browser viewport changed canonical tmux geometry: "
                    f"before={before_size}, compact={compact_size}, after={after_size}"
                )
            browser_grid = page_one.evaluate(
                """() => {
                  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
                  return terminal ? `${terminal.cols}x${terminal.rows}` : null;
                }"""
            )
            if browser_grid != before_size:
                raise RuntimeError(
                    f"browser xterm grid diverged from canonical tmux geometry: "
                    f"tmux={before_size}, browser={browser_grid}"
                )
            result["resize"] = {
                "canonical": before_size,
                "after_compact_viewport": compact_size,
                "after_large_viewport": after_size,
                "browser_grid": browser_grid,
                "isolated": True,
            }
            type_command(page_one, "printf 'RESIZE_ISOLATED_OK\n'", "RESIZE_ISOLATED_OK")

            result["takeover"] = {
                "covered_by": "check_web_browser_e2e.py",
                "reason": "the authoritative browser gate uses two independent Chromium processes and manual stale-writer frames",
            }

            server.stop()
            page_one.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent?.startsWith('Reconnecting')",
                timeout=15_000,
            )
            time.sleep(0.5)
            result["server_restart_ms"] = round(server.start(), 3)
            page_one.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=30_000,
            )
            type_command(page_one, "printf 'RECONNECT_OK\\n'", "RECONNECT_OK", 15_000)
            result["reconnect_resync"] = True

            page_one.get_by_role("button", name="New tab", exact=True).click()
            dialog = page_one.locator("dialog[open]")
            dialog.locator('input[name="cwd"]').fill(str(root))
            dialog.locator('input[name="name"]').fill("Browser lifecycle")
            dialog.locator('input[name="command"]').fill("printf 'NEW_TAB_BOOT_OK\\n'")
            dialog.locator('button[type="submit"]').click()
            page_one.locator(".workspace-tab-title", has_text="Browser lifecycle").wait_for(
                state="visible", timeout=20_000
            )
            new_url = page_one.url
            if "/random/t/td-e2e-" not in new_url:
                raise RuntimeError(f"new-tab route did not use stable pane URL: {new_url}")
            created_id = new_url.rsplit("/", 1)[-1]
            wait_buffer(page_one, "NEW_TAB_BOOT_OK", 15_000)
            type_command(page_one, "printf 'NEW_TAB_INPUT_OK\\n'", "NEW_TAB_INPUT_OK")

            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.once("dialog", lambda prompt: prompt.accept("Renamed browser tab"))
            page_one.locator(".workspace-action-panel button", has_text="Rename tab").click()
            page_one.locator(".workspace-tab-title", has_text="Renamed browser tab").wait_for(
                state="visible", timeout=15_000
            )
            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.get_by_role("button", name="Pin", exact=True).click()
            wait_until(lambda: (registry_row(database, created_id) or {}).get("pinned") == 1)
            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.locator(".workspace-action-panel button", has_text="Unpin").click()
            wait_until(lambda: (registry_row(database, created_id) or {}).get("pinned") == 0)
            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.locator(".workspace-action-panel button", has_text="Pause").click()
            wait_until(lambda: (registry_row(database, created_id) or {}).get("status") == "paused")
            page_one.reload(wait_until="domcontentloaded", timeout=15_000)
            page_one.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=15_000)
            page_one.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=15_000,
            )
            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.locator(".workspace-action-panel button", has_text="Resume").click()
            wait_until(lambda: (registry_row(database, created_id) or {}).get("status") == "running")
            hidden = registry_row(database, created_id)
            if hidden is None:
                raise RuntimeError("created tab vanished before hide")
            hidden_tmux = hidden["tmux_name"]
            page_one.locator(f'.workspace-tab[data-pane-id="{created_id}"] .workspace-tab-close').click()
            wait_until(
                lambda: (registry_row(database, created_id) or {}).get("visible") == 0,
                timeout=15.0,
            )
            page_one.locator(".workspace-tab-title", has_text="Renamed browser tab").wait_for(
                state="detached", timeout=15_000
            )
            if subprocess.run(
                ["/usr/bin/tmux", "-L", socket_name, "has-session", "-t", hidden_tmux],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode != 0:
                raise RuntimeError("native close button killed the hidden terminal")

            page_one.get_by_role("button", name="New tab", exact=True).click()
            kill_dialog = page_one.locator("dialog[open]")
            kill_dialog.locator('input[name="cwd"]').fill(str(root))
            kill_dialog.locator('input[name="name"]').fill("Kill target")
            kill_dialog.locator('button[type="submit"]').click()
            page_one.locator(".workspace-tab-title", has_text="Kill target").wait_for(
                state="visible", timeout=20_000
            )
            kill_id = page_one.url.rsplit("/", 1)[-1]
            kill_row = registry_row(database, kill_id)
            if kill_row is None:
                raise RuntimeError("kill target was not registered")
            kill_tmux = kill_row["tmux_name"]
            page_one.locator('summary[aria-label="Tab actions"]').click()
            page_one.once("dialog", lambda confirmation: confirmation.accept())
            page_one.locator(".workspace-action-panel button", has_text="Kill process…").click()
            wait_until(
                lambda: (registry_row(database, kill_id) or {}).get("status") == "stopped"
                and (registry_row(database, kill_id) or {}).get("visible") == 0,
                timeout=15.0,
            )
            if subprocess.run(
                ["/usr/bin/tmux", "-L", socket_name, "has-session", "-t", kill_tmux],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode == 0:
                raise RuntimeError("explicit kill left its disposable process alive")
            if "/random/t/td-fixture" not in page_one.url:
                page_one.locator('.workspace-tab[data-pane-id="td-fixture"] .workspace-tab-select').click()
            wait_buffer(page_one, "RECONNECT_OK", 10_000)
            result["tab_lifecycle"] = {
                "created": created_id,
                "renamed": True,
                "pin_unpin": True,
                "pause_resume": True,
                "hidden_without_kill": True,
                "killed_explicitly": True,
                "new_tab_button": True,
            }

            screenshot = evidence / "terminal-e2e.png"
            page_one.screenshot(path=str(screenshot), full_page=True)
            result["screenshot"] = {
                "path": str(screenshot),
                "bytes": screenshot.stat().st_size,
                "sha256": sha256_file(screenshot),
            }
            final_buffer = buffer_text(page_one)
            if "RECONNECT_OK" not in final_buffer:
                raise RuntimeError("final terminal buffer lost the post-reconnect marker")
            result["final_buffer_sha256"] = hashlib.sha256(final_buffer.encode()).hexdigest()
            result["final_buffer_tail"] = final_buffer[-12_000:]
            context_one.close()
            browser_one.close()
            contexts.remove(context_one)
            browsers.remove(browser_one)

        def expected_reconnect_noise(value: str) -> bool:
            return any(
                marker in value
                for marker in (
                    "ERR_CONNECTION_REFUSED",
                    "Failed to load resource",
                    "WebSocket is closed before the connection is established",
                )
            )

        unexpected_console = [
            item
            for item in console
            if (
                item.get("type") in {"error", "assert"}
                and not expected_reconnect_noise(item.get("text", ""))
            )
            or (
                item.get("type") == "warning"
                and not any(
                    marker in item.get("text", "")
                    for marker in (
                        "GL Driver Message",
                        "Automatic fallback to software WebGL has been deprecated",
                    )
                )
            )
        ]
        if unexpected_console:
            raise RuntimeError(f"unexpected browser console output: {unexpected_console!r}")
        if page_errors:
            raise RuntimeError(f"browser page errors: {page_errors!r}")
        unexpected_request_failures = [
            failure for failure in request_failures if not expected_reconnect_noise(failure)
        ]
        if unexpected_request_failures:
            raise RuntimeError(f"browser request failures: {unexpected_request_failures!r}")
        websocket_errors = [
            error
            for record in websocket_records
            for error in record["errors"]
            if not expected_reconnect_noise(str(error))
        ]
        if not websocket_records or websocket_errors:
            raise RuntimeError(
                f"WebSocket evidence is incomplete: records={websocket_records!r} errors={websocket_errors!r}"
            )
        if result.get("terminal_asset_cache_control") != "public, max-age=86400, immutable":
            raise RuntimeError(
                f"versioned terminal asset cache policy mismatch: {result.get('terminal_asset_cache_control')!r}"
            )
        csp = result.get("content_security_policy")
        if not isinstance(csp, str) or "script-src 'self'" not in csp or "'unsafe-eval'" in csp:
            raise RuntimeError(f"CSP mismatch: {csp!r}")
        result["console"] = console
        result["page_errors"] = page_errors
        result["request_failures"] = request_failures
        result["websockets"] = websocket_records
        result["ok"] = True
        result_path = evidence / "result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        for context in reversed(contexts):
            try:
                context.close()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
        for browser in reversed(browsers):
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
        if server is not None:
            server.stop()
        subprocess.run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        shutil.rmtree(run_root, ignore_errors=True)
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp


if __name__ == "__main__":
    raise SystemExit(main())
