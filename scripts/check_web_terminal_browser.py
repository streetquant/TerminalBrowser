#!/usr/bin/env python3
"""Disposable Chromium proof for the interactive TermDeck browser client.

The proof is deliberately incapable of addressing the default tmux server or
production registry.  It creates a private HOME, TMUX_TMPDIR, SQLite registry,
auth store, tmux server, web server, and Chromium profile, then removes them.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

try:
    from playwright.sync_api import BrowserContext, Page, sync_playwright
except ImportError as exc:  # pragma: no cover - environment gate
    raise SystemExit("playwright is required for the TermDeck browser proof") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / ".ai-bridge/target/debug/termdeck-web-server"
REJECTED_UI = (
    "Take control",
    "Take over control",
    "Release control",
    "Send text",
    "Ctrl+C",
    "Ctrl+D",
    "Ctrl+L",
)

SOCKET_CAPTURE_INIT = r"""
(() => {
  const NativeWebSocket = window.WebSocket;
  const sockets = [];
  function WrappedWebSocket(...args) {
    const socket = new NativeWebSocket(...args);
    sockets.push(socket);
    return socket;
  }
  WrappedWebSocket.prototype = NativeWebSocket.prototype;
  Object.setPrototypeOf(WrappedWebSocket, NativeWebSocket);
  for (const name of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
    Object.defineProperty(WrappedWebSocket, name, { value: NativeWebSocket[name] });
  }
  Object.defineProperty(window, "__termdeckTestSockets", { value: sockets });
  window.WebSocket = WrappedWebSocket;
})();
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate: Callable[[], bool], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except BaseException as exc:  # noqa: BLE001 - retained for diagnostic timeout
            last_error = exc
        time.sleep(0.05)
    suffix = f": {last_error}" if last_error else ""
    raise AssertionError(f"timed out waiting for {detail}{suffix}")


def wait_http(url: str, *, timeout: float = 20.0) -> None:
    def ready() -> bool:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            return response.status == 200

    wait_until(ready, timeout=timeout, detail=f"HTTP readiness at {url}")


def run_json(argv: list[str], *, env: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {shlex.join(argv)}\n"
            f"stdout={completed.stdout[-4000:]!r}\nstderr={completed.stderr[-4000:]!r}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"command did not return JSON: {shlex.join(argv)}\n{completed.stdout[-4000:]}"
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"command returned non-object JSON: {shlex.join(argv)}")
    return payload


def write_fixture(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r'''
            from __future__ import annotations

            import fcntl
            import os
            from pathlib import Path
            import signal
            import struct
            import sys
            import termios
            import tty

            raw_log = Path(sys.argv[1])
            event_log = Path(sys.argv[2])
            label = sys.argv[3]
            raw_log.parent.mkdir(parents=True, exist_ok=True)
            event_log.parent.mkdir(parents=True, exist_ok=True)
            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)

            def append_event(value: str) -> None:
                with event_log.open("a", encoding="utf-8") as handle:
                    handle.write(value + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

            def geometry() -> tuple[int, int]:
                rows, cols, _xp, _yp = struct.unpack(
                    "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
                )
                return cols, rows

            def resized(_signum: int, _frame: object) -> None:
                cols, rows = geometry()
                append_event(f"RESIZE {cols}x{rows}")

            signal.signal(signal.SIGWINCH, resized)
            tty.setraw(fd)
            try:
                cols, rows = geometry()
                append_event(f"START {label} {cols}x{rows}")
                screen = (
                    b"\x1b[?1049h\x1b[2J\x1b[H"
                    + b"\x1b[31mRED\x1b[0m ANSI_COLOR_OK\r\n"
                    + "UNICODE_OK 東京🙂 COMBINING=é\r\n".encode("utf-8")
                    + f"CODEX COMPOSER › {label}\r\n".encode("utf-8")
                    + b"KEYBOARD_PASTE_MOUSE_RESIZE_READY\r\n"
                    + b"\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h"
                )
                os.write(sys.stdout.fileno(), screen)
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    with raw_log.open("ab", buffering=0) as handle:
                        handle.write(data)
                        os.fsync(handle.fileno())
            finally:
                try:
                    os.write(
                        sys.stdout.fileno(),
                        b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?2004l\x1b[?1049l",
                    )
                finally:
                    termios.tcsetattr(fd, termios.TCSANOW, original)
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_wrappers(
    bin_dir: Path,
    *,
    home: Path,
    tmux_tmpdir: Path,
    python: Path,
) -> tuple[Path, Path]:
    bin_dir.mkdir(parents=True, mode=0o700)
    tmux_wrapper = bin_dir / "tmux"
    tmux_wrapper.write_text(
        "#!/bin/sh\n"
        f"export HOME={shlex.quote(str(home))}\n"
        f"export TMUX_TMPDIR={shlex.quote(str(tmux_tmpdir))}\n"
        "unset TMUX TMUX_PANE\n"
        "exec /usr/bin/tmux \"$@\"\n",
        encoding="utf-8",
    )
    tmux_wrapper.chmod(0o700)
    t1000_wrapper = bin_dir / "t1000"
    t1000_wrapper.write_text(
        "#!/bin/sh\n"
        f"export HOME={shlex.quote(str(home))}\n"
        f"export XDG_CONFIG_HOME={shlex.quote(str(home / '.config'))}\n"
        f"export XDG_DATA_HOME={shlex.quote(str(home / '.local/share'))}\n"
        f"export XDG_CACHE_HOME={shlex.quote(str(home / '.cache'))}\n"
        f"export TMUX_TMPDIR={shlex.quote(str(tmux_tmpdir))}\n"
        f"export PYTHONPATH={shlex.quote(str(ROOT))}\n"
        "export PYTHONNOUSERSITE=1 NO_AT_BRIDGE=1\n"
        "unset TMUX TMUX_PANE\n"
        f"exec {shlex.quote(str(python))} -B -m termdeck \"$@\"\n",
        encoding="utf-8",
    )
    t1000_wrapper.chmod(0o700)
    return tmux_wrapper, t1000_wrapper


def invitation(binary: Path, auth_dir: Path, label: str) -> str:
    output = subprocess.check_output(
        [
            str(binary),
            "--auth-dir",
            str(auth_dir),
            "--host-id",
            "host-browser-e2e",
            "--owner-id",
            "owner-browser-e2e",
            "--create-invitation",
            label,
        ],
        cwd=ROOT,
        text=True,
        timeout=20,
    )
    payload = json.loads(output)
    secret = payload.get("invitation_secret")
    if not isinstance(secret, str) or not secret:
        raise AssertionError("invitation command did not return a secret")
    return secret


def pair_cookies(origin: str, secret: str) -> list[dict[str, Any]]:
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
            "Sec-Fetch-Dest": "document",
        },
    )
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise AssertionError(f"pairing failed with HTTP {response.status}")
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
    names = {cookie["name"] for cookie in cookies}
    if names != {"termdeck_session", "termdeck_csrf"}:
        raise AssertionError(f"pairing returned unexpected cookies: {sorted(names)}")
    return cookies


def wait_bytes(path: Path, needle: bytes, *, timeout: float = 8.0) -> bytes:
    current = b""

    def present() -> bool:
        nonlocal current
        current = path.read_bytes() if path.exists() else b""
        return needle in current

    wait_until(present, timeout=timeout, detail=f"terminal bytes {needle!r}")
    return current


def tmux_geometry(tmux: Path, env: dict[str, str], target: str) -> str:
    completed = subprocess.run(
        [str(tmux), "display-message", "-p", "-t", target, "#{pane_width}x#{pane_height}"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"tmux geometry failed: {completed.stderr.strip()}")
    value = completed.stdout.strip()
    if "x" not in value:
        raise AssertionError(f"invalid tmux geometry: {value!r}")
    return value


def database_row(database: Path, session_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id,name,status,visible,pinned,window_id,window_title,tmux_name FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def click_action(page: Page, action: str) -> None:
    page.locator('summary[aria-label="Tab actions"]').click()
    page.get_by_role("button", name=action, exact=True).click()


def terminal_state(page: Page) -> dict[str, Any]:
    payload = page.evaluate(
        r"""() => {
          const api = document.querySelector('#terminal-xterm')?.termdeckTerminal;
          if (!api?.terminal?.buffer?.active) return null;
          const buffer = api.terminal.buffer.active;
          const read = (candidate) => {
            const lines = [];
            for (let y = 0; y < candidate.length; y += 1) {
              lines.push(candidate.getLine(y)?.translateToString(true) || '');
            }
            return lines.join('\n');
          };
          let red = null;
          for (let y = 0; y < buffer.length; y += 1) {
            const line = buffer.getLine(y);
            const text = line?.translateToString(true) || '';
            const index = text.indexOf('RED');
            if (index >= 0) {
              const cell = line.getCell(index);
              red = cell ? {
                fgDefault: cell.isFgDefault(),
                fgPalette: cell.isFgPalette(),
                fgRgb: cell.isFgRGB(),
                fgColor: cell.getFgColor(),
              } : null;
              break;
            }
          }
          return {
            type: buffer.type,
            text: api.readBuffer(),
            normalText: read(api.terminal.buffer.normal),
            alternateText: read(api.terminal.buffer.alternate),
            cols: api.terminal.cols,
            rows: api.terminal.rows,
            red,
          };
        }"""
    )
    if not isinstance(payload, dict):
        raise AssertionError("xterm public buffer API is unavailable")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--chromium", type=Path, default=Path(shutil.which("chromium") or "/usr/bin/chromium"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-live-ms", type=float, default=5000.0)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    binary = args.server_binary.resolve()
    chromium = args.chromium.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(f"server binary is not executable: {binary}")
    if not chromium.is_file() or not os.access(chromium, os.X_OK):
        raise SystemExit(f"Chromium is not executable: {chromium}")

    run_root = Path(tempfile.mkdtemp(prefix="termdeck-web-browser-e2e-"))
    home = run_root / "home"
    for directory in (
        home,
        home / ".config",
        home / ".local/share",
        home / ".cache",
        run_root / "tmux",
        run_root / "logs",
        run_root / "auth-parent",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = run_root / "sessions.sqlite3"
    auth_dir = run_root / "auth-parent/auth"
    fixture = run_root / "fixture_terminal.py"
    initial_raw = run_root / "logs/initial.raw"
    initial_events = run_root / "logs/initial.events"
    created_raw = run_root / "logs/created.raw"
    created_events = run_root / "logs/created.events"
    kill_raw = run_root / "logs/kill.raw"
    kill_events = run_root / "logs/kill.events"
    write_fixture(fixture)
    tmux, t1000 = write_wrappers(
        run_root / "bin",
        home=home,
        tmux_tmpdir=run_root / "tmux",
        python=args.python.resolve(),
    )
    base_env = os.environ.copy()
    base_env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TMUX_TMPDIR": str(run_root / "tmux"),
            "PYTHONPATH": str(ROOT),
            "PYTHONNOUSERSITE": "1",
            "NO_AT_BRIDGE": "1",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "PATH": f"{run_root / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        }
    )
    base_env.pop("TMUX", None)
    base_env.pop("TMUX_PANE", None)

    def cleanup_private_fixture() -> None:
        if tmux.exists():
            subprocess.run(
                [str(tmux), "kill-server"],
                env=base_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        if not args.keep_temp:
            shutil.rmtree(run_root, ignore_errors=True)

    atexit.register(cleanup_private_fixture)

    initial_command = shlex.join(
        [str(args.python.resolve()), "-u", str(fixture), str(initial_raw), str(initial_events), "PRIMARY"]
    )
    initial_payload = run_json(
        [
            str(t1000),
            "new",
            "--db",
            str(database),
            "--cwd",
            str(run_root),
            "--window-id",
            "win-random",
            "--window-title",
            "Random",
            "--name",
            "Codex fixture",
            "--command",
            initial_command,
        ],
        env=base_env,
        timeout=40,
    )
    tabs = initial_payload.get("tabs")
    if initial_payload.get("ok") is not True or not isinstance(tabs, list) or len(tabs) != 1:
        raise AssertionError(f"initial tab creation failed: {initial_payload!r}")
    initial_tab = tabs[0]
    initial_id = initial_tab["id"]
    initial_row = database_row(database, initial_id)
    if not initial_row:
        raise AssertionError(f"initial registry row is absent: {initial_id}")
    initial_tmux_name = str(initial_row["tmux_name"])
    wait_until(initial_events.exists, timeout=10, detail="fixture startup")

    def initial_screen_ready() -> bool:
        completed = subprocess.run(
            [
                str(tmux),
                "capture-pane",
                "-epq",
                "-t",
                f"{initial_tmux_name}:0.0",
                "-N",
            ],
            env=base_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        return (
            completed.returncode == 0
            and "KEYBOARD_PASTE_MOUSE_RESIZE_READY" in completed.stdout
        )

    wait_until(initial_screen_ready, timeout=10, detail="fixture screen readiness")

    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    first_secret = invitation(binary, auth_dir, "Browser One")
    second_secret = invitation(binary, auth_dir, "Browser Two")
    server_stdout = output / "server.stdout"
    server_stderr = output / "server.stderr"
    server: subprocess.Popen[bytes] | None = None

    def start_server() -> subprocess.Popen[bytes]:
        stdout_handle = server_stdout.open("ab")
        stderr_handle = server_stderr.open("ab")
        process = subprocess.Popen(
            [
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
                "128",
                "--tmux",
                str(tmux),
                "--tmux-timeout-ms",
                "3000",
                "--t1000",
                str(t1000),
                "--tab-action-timeout-ms",
                "30000",
                "--host-id",
                "host-browser-e2e",
                "--owner-id",
                "owner-browser-e2e",
                "--auth-dir",
                str(auth_dir),
                "--allow-insecure-loopback-auth",
            ],
            cwd=ROOT,
            env=base_env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        stdout_handle.close()
        stderr_handle.close()
        wait_http(f"{origin}/_termdeck/healthz", timeout=20)
        return process

    def stop_server(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    result: dict[str, Any] = {
        "schema": "termdeck.web-terminal-browser-proof.v1",
        "source": {
            "server_binary": str(binary),
            "server_sha256": sha256_file(binary),
        },
        "assertions": {},
        "timings_ms": {},
        "lifecycle": {},
        "control": {},
        "geometry": {},
        "console": [],
        "page_errors": [],
        "request_failures": [],
        "websockets": {"one": [], "two": []},
    }

    try:
        server = start_server()
        cookies_one = pair_cookies(origin, first_secret)
        cookies_two = pair_cookies(origin, second_secret)
        # Secrets are no longer needed and are intentionally dropped before evidence creation.
        first_secret = ""
        second_secret = ""

        browser_home = run_root / "browser-home"
        (browser_home / ".config").mkdir(parents=True, mode=0o700)
        os.environ["HOME"] = str(browser_home)
        os.environ["XDG_CONFIG_HOME"] = str(browser_home / ".config")
        os.environ["TMPDIR"] = "/tmp"
        tempfile.tempdir = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(chromium),
                headless=True,
                args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
            )
            context_one = browser.new_context(viewport={"width": 1440, "height": 900})
            context_two = browser.new_context(viewport={"width": 1280, "height": 800})
            context_one.add_init_script(SOCKET_CAPTURE_INIT)
            context_two.add_init_script(SOCKET_CAPTURE_INIT)
            context_one.add_cookies(cookies_one)
            context_two.add_cookies(cookies_two)
            page_one = context_one.new_page()
            page_two = context_two.new_page()

            def instrument(page: Page, label: str) -> None:
                page.on(
                    "console",
                    lambda message: result["console"].append(
                        {"page": label, "type": message.type, "text": message.text[:2000]}
                    ),
                )
                page.on(
                    "pageerror",
                    lambda error: result["page_errors"].append(
                        {"page": label, "error": str(error)[:4000]}
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request: result["request_failures"].append(
                        {"page": label, "url": request.url, "failure": str(request.failure)}
                    ),
                )

                def on_websocket(websocket: Any) -> None:
                    record: dict[str, Any] = {
                        "url": websocket.url,
                        "events": [],
                        "binary_frames": 0,
                        "binary_bytes": 0,
                        "closed": False,
                        "socket_errors": [],
                    }
                    result["websockets"][label].append(record)

                    def receive(payload: str | bytes) -> None:
                        if isinstance(payload, bytes):
                            record["binary_frames"] += 1
                            record["binary_bytes"] += len(payload)
                            if "binary_prefix_hex" not in record:
                                record["binary_prefix_hex"] = payload[:4096].hex()
                            return
                        try:
                            event = json.loads(payload)
                        except (json.JSONDecodeError, TypeError):
                            record["events"].append({"type": "unparsed", "bytes": len(str(payload))})
                            return
                        if isinstance(event, dict):
                            # Protocol events contain public device IDs and sequence metadata,
                            # never bearer secrets. Keep only bounded structured evidence.
                            record["events"].append(event)

                    websocket.on("framereceived", receive)
                    websocket.on("close", lambda: record.__setitem__("closed", True))
                    websocket.on(
                        "socketerror",
                        lambda error: record["socket_errors"].append(str(error)[:1000]),
                    )

                page.on("websocket", on_websocket)

            def latest_socket_control(label: str) -> dict[str, Any] | None:
                for record in reversed(result["websockets"][label]):
                    for event in reversed(record["events"]):
                        control = event.get("control") if isinstance(event, dict) else None
                        if isinstance(control, dict) and control.get("schema") == "termdeck.terminal-control.v1":
                            return control
                return None

            def socket_error_count(label: str) -> int:
                total = 0
                for record in result["websockets"][label]:
                    total += len(record["socket_errors"])
                    total += sum(
                        1
                        for event in record["events"]
                        if isinstance(event, dict) and event.get("type") == "error"
                    )
                return total

            def any_socket_closed(label: str) -> bool:
                return any(record.get("closed") is True for record in result["websockets"][label])

            def wait_browser_condition(
                page: Page,
                predicate: Callable[[], bool],
                *,
                timeout: float,
                detail: str,
            ) -> None:
                deadline = time.monotonic() + timeout
                last_error: BaseException | None = None
                while time.monotonic() < deadline:
                    # Sync Playwright dispatches WebSocket callbacks while a browser
                    # API call pumps its protocol event loop. Plain time.sleep does not.
                    page.wait_for_timeout(50)
                    try:
                        if predicate():
                            return
                    except BaseException as exc:  # noqa: BLE001 - diagnostic boundary
                        last_error = exc
                suffix = f": {last_error}" if last_error else ""
                raise AssertionError(f"timed out waiting for {detail}{suffix}")

            def send_manual_frame(page: Page, sequence: int, text: str) -> None:
                page.evaluate(
                    r"""({sequence, text}) => {
                      const sockets = window.__termdeckTestSockets || [];
                      const socket = [...sockets].reverse().find(
                        candidate => candidate.readyState === WebSocket.OPEN
                      );
                      if (!socket) throw new Error('no open terminal socket');
                      const body = new TextEncoder().encode(text);
                      const frame = new Uint8Array(9 + body.length);
                      frame[0] = 1;
                      new DataView(frame.buffer).setBigUint64(1, BigInt(sequence), false);
                      frame.set(body, 9);
                      socket.send(frame);
                    }""",
                    {"sequence": sequence, "text": text},
                )

            instrument(page_one, "one")
            instrument(page_two, "two")

            api_url = f"{origin}/_termdeck/api/v1/windows/random"
            page_one.route(api_url, lambda route: route.abort("failed"))
            started = time.monotonic_ns()
            page_one.goto(f"{origin}/random", wait_until="domcontentloaded", timeout=15_000)
            page_one.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=20_000)
            xterm_ms = (time.monotonic_ns() - started) / 1_000_000
            try:
                page_one.wait_for_function(
                    "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                    timeout=20_000,
                )
            except Exception:
                result["live_timeout_diagnostic"] = {
                    "status": page_one.locator("#terminal-status").text_content(),
                    "terminal": terminal_state(page_one),
                }
                raise
            live_ms = (time.monotonic_ns() - started) / 1_000_000
            result["timings_ms"].update({"xterm_visible": round(xterm_ms, 3), "terminal_live": round(live_ms, 3)})
            if xterm_ms > args.max_live_ms or live_ms > args.max_live_ms:
                raise AssertionError(
                    f"terminal startup exceeded {args.max_live_ms:g} ms: xterm={xterm_ms:.1f}, live={live_ms:.1f}"
                )
            if urllib.parse.urlparse(page_one.url).path != "/random":
                raise AssertionError(f"window route changed unexpectedly: {page_one.url}")
            page_one.unroute(api_url)
            page_one.wait_for_function(
                "() => document.querySelectorAll('.workspace-tab').length === 1",
                timeout=10_000,
            )
            result["assertions"]["terminal_starts_without_window_api"] = True

            try:
                page_one.wait_for_function(
                    "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('KEYBOARD_PASTE_MOUSE_RESIZE_READY')",
                    timeout=20_000,
                )
            except Exception:
                result["initial_terminal_diagnostic"] = terminal_state(page_one)
                raise
            state = terminal_state(page_one)
            if state.get("type") != "alternate":
                raise AssertionError(f"xterm did not restore alternate screen: {state!r}")
            text = str(state.get("text", ""))
            for marker in ("ANSI_COLOR_OK", "UNICODE_OK 東京🙂", "COMBINING=é", "CODEX COMPOSER › PRIMARY"):
                if marker not in text:
                    raise AssertionError(f"xterm buffer is missing {marker!r}: {text[-4000:]!r}")
            red = state.get("red")
            if not isinstance(red, dict) or red.get("fgDefault") is not False:
                raise AssertionError(f"ANSI foreground was not preserved: {red!r}")
            result["assertions"].update(
                {
                    "alternate_screen": True,
                    "ansi_color": True,
                    "unicode_and_combining": True,
                    "codex_composer_rendering": True,
                }
            )

            html = page_one.content()
            present = [value for value in REJECTED_UI if value in html]
            if present:
                raise AssertionError(f"rejected manual terminal UI is present: {present}")
            result["assertions"]["no_manual_control_or_key_ui"] = True

            terminal_box = page_one.locator("#terminal-xterm").bounding_box()
            if not terminal_box:
                raise AssertionError("terminal has no browser geometry")
            page_one.mouse.click(terminal_box["x"] + 100, terminal_box["y"] + 100)
            page_one.keyboard.type("ONE_KEYBOARD")
            page_one.keyboard.press("Enter")
            page_one.keyboard.press("Control+C")
            page_one.keyboard.press("ArrowUp")
            page_one.keyboard.press("Tab")
            wait_bytes(initial_raw, b"ONE_KEYBOARD\r")
            raw = wait_bytes(initial_raw, b"\x03")
            if b"\x1b[A" not in raw and b"\x1bOA" not in raw:
                wait_until(
                    lambda: b"\x1b[A" in initial_raw.read_bytes() or b"\x1bOA" in initial_raw.read_bytes(),
                    timeout=5,
                    detail="arrow-key bytes",
                )
            wait_bytes(initial_raw, b"\t")
            result["assertions"]["keyboard_control_bytes"] = True

            paste_text = "PASTE_東京🙂"
            page_one.evaluate(
                "text => document.querySelector('#terminal-xterm').termdeckTerminal.paste(text)",
                paste_text,
            )
            paste_bytes = b"\x1b[200~" + paste_text.encode("utf-8") + b"\x1b[201~"
            wait_bytes(initial_raw, paste_bytes)
            result["assertions"]["bracketed_paste"] = True

            mouse_mode = page_one.evaluate(
                "() => document.querySelector('#terminal-xterm').termdeckTerminal.terminal.modes.mouseTrackingMode"
            )
            pane_mouse_state = subprocess.check_output(
                [
                    str(tmux),
                    "display-message",
                    "-p",
                    "-t",
                    initial_tmux_name,
                    "#{mouse_standard_flag}:#{mouse_button_flag}:#{mouse_all_flag}:#{mouse_sgr_flag}",
                ],
                env=base_env,
                text=True,
                timeout=5,
            ).strip()
            result["mouse"] = {"xterm_mode": mouse_mode, "tmux_flags": pane_mouse_state}
            if mouse_mode == "none":
                raise AssertionError(f"xterm mouse tracking was not restored: tmux={pane_mouse_state}")
            screen_box = page_one.locator("#terminal-xterm .xterm-screen").bounding_box() or terminal_box
            before_mouse = initial_raw.read_bytes()
            page_one.mouse.move(screen_box["x"] + 160, screen_box["y"] + 140)
            page_one.mouse.down(button="left")
            page_one.mouse.move(screen_box["x"] + 190, screen_box["y"] + 150, steps=3)
            page_one.mouse.up(button="left")
            page_one.mouse.wheel(0, 120)
            wait_until(
                lambda: b"\x1b[<" in initial_raw.read_bytes()[len(before_mouse) :],
                timeout=8,
                detail=f"SGR mouse bytes (mode={mouse_mode}, tmux={pane_mouse_state})",
            )
            result["assertions"]["mouse_protocol"] = True

            geometry_before = tmux_geometry(tmux, base_env, initial_tmux_name)
            page_one.set_viewport_size({"width": 980, "height": 620})
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) != geometry_before,
                timeout=8,
                detail="smaller terminal geometry",
            )
            geometry_small = tmux_geometry(tmux, base_env, initial_tmux_name)
            page_one.set_viewport_size({"width": 1600, "height": 1000})
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) not in {geometry_before, geometry_small},
                timeout=8,
                detail="larger terminal geometry",
            )
            geometry_large = tmux_geometry(tmux, base_env, initial_tmux_name)
            result["geometry"] = {
                "initial": geometry_before,
                "small": geometry_small,
                "large": geometry_large,
            }
            result["assertions"]["resize_propagation"] = True

            wait_browser_condition(
                page_one,
                lambda: (latest_socket_control("one") or {}).get("mine") is True,
                timeout=5,
                detail="first-browser WebSocket control ownership",
            )
            first_view = latest_socket_control("one") or {}
            page_two.goto(f"{origin}/random/t/{urllib.parse.quote(initial_id)}", wait_until="domcontentloaded", timeout=15_000)
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=10_000,
            )
            wait_browser_condition(
                page_two,
                lambda: latest_socket_control("two") is not None,
                timeout=5,
                detail="second-browser WebSocket ready control state",
            )
            second_before = latest_socket_control("two") or {}
            if second_before.get("mine") is not True:
                raise AssertionError(f"second browser did not automatically own the pane: {second_before!r}")
            if int(second_before.get("epoch") or 0) <= int(first_view.get("epoch") or 0):
                raise AssertionError(
                    f"opening the second browser did not advance controller epoch: "
                    f"first={first_view!r}, second={second_before!r}"
                )
            box_two = page_two.locator("#terminal-xterm").bounding_box()
            if not box_two:
                raise AssertionError("second terminal has no browser geometry")
            page_two.mouse.click(box_two["x"] + 100, box_two["y"] + 100)
            page_two.keyboard.type("TWO_TAKEOVER")
            page_two.keyboard.press("Enter")
            wait_bytes(initial_raw, b"TWO_TAKEOVER\r")
            second_after = latest_socket_control("two") or {}
            if second_after.get("mine") is not True:
                raise AssertionError(
                    f"second browser lost ownership before input: {second_after!r}"
                )
            stale_offset = len(initial_raw.read_bytes())
            stale_error_offset = socket_error_count("one")
            stale_closed_before = any_socket_closed("one")
            stale_sequence = int(first_view.get("next_sequence") or 1)
            send_manual_frame(page_one, stale_sequence, "STALE_WRITER_REJECTED")
            wait_browser_condition(
                page_one,
                lambda: socket_error_count("one") > stale_error_offset
                or (not stale_closed_before and any_socket_closed("one"))
                or (latest_socket_control("one") or {}).get("mine") is False,
                timeout=8,
                detail="generation-stale writer protocol rejection",
            )
            time.sleep(0.25)
            stale_suffix = initial_raw.read_bytes()[stale_offset:]
            if b"STALE_WRITER_REJECTED" in stale_suffix:
                raise AssertionError("stale writer bytes reached tmux after takeover")
            result["control"] = {
                "first_initial_epoch": first_view.get("epoch"),
                "first_stale_sequence": stale_sequence,
                "second_auto_takeover_epoch": second_before.get("epoch"),
                "second_input_epoch": second_after.get("epoch"),
                "automatic_takeover": True,
                "stale_writer_rejected": True,
            }
            result["assertions"]["two_browser_takeover_and_stale_rejection"] = True

            page_two.reload(wait_until="domcontentloaded", timeout=15_000)
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=10_000,
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('CODEX COMPOSER › PRIMARY')",
                timeout=10_000,
            )
            page_two.locator("#terminal-xterm").click(position={"x": 120, "y": 120})
            page_two.keyboard.type("AFTER_RELOAD_SEQUENCE")
            page_two.keyboard.press("Enter")
            wait_bytes(initial_raw, b"AFTER_RELOAD_SEQUENCE\r")
            result["assertions"]["reload_resynchronization"] = True

            stop_server(server)
            server = None
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent.includes('Reconnecting')",
                timeout=10_000,
            )
            server = start_server()
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=20_000,
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('CODEX COMPOSER › PRIMARY')",
                timeout=10_000,
            )
            page_two.locator("#terminal-xterm").click(position={"x": 120, "y": 120})
            page_two.keyboard.type("AFTER_SERVER_RESTART")
            page_two.keyboard.press("Enter")
            wait_bytes(initial_raw, b"AFTER_SERVER_RESTART\r")
            result["assertions"]["server_restart_reconnect_and_resync"] = True

            created_command = shlex.join(
                [
                    str(args.python.resolve()),
                    "-u",
                    str(fixture),
                    str(created_raw),
                    str(created_events),
                    "CREATED",
                ]
            )
            page_two.get_by_role("button", name="New tab", exact=True).click()
            page_two.locator('dialog input[name="cwd"]').fill(str(run_root))
            page_two.locator('dialog input[name="name"]').fill("Browser Created")
            page_two.locator('dialog input[name="command"]').fill(created_command)
            page_two.get_by_role("button", name="Create tab", exact=True).click()
            page_two.wait_for_url(f"**/random/t/*", timeout=40_000)
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=20_000,
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('CODEX COMPOSER › CREATED')",
                timeout=15_000,
            )
            created_id = urllib.parse.unquote(urllib.parse.urlparse(page_two.url).path.rsplit("/", 1)[-1])
            created = database_row(database, created_id)
            if not created or created["status"] != "running" or created["visible"] != 1:
                raise AssertionError(f"browser-created tab is not running: {created!r}")
            result["lifecycle"]["created_id"] = created_id

            page_two.once("dialog", lambda dialog: dialog.accept("Renamed Browser Tab"))
            click_action(page_two, "Rename tab")
            wait_until(
                lambda: (database_row(database, created_id) or {}).get("name") == "Renamed Browser Tab",
                timeout=8,
                detail="tab rename",
            )
            click_action(page_two, "Pin")
            wait_until(
                lambda: (database_row(database, created_id) or {}).get("pinned") == 1,
                timeout=8,
                detail="tab pin",
            )
            click_action(page_two, "Unpin")
            wait_until(
                lambda: (database_row(database, created_id) or {}).get("pinned") == 0,
                timeout=8,
                detail="tab unpin",
            )
            click_action(page_two, "Pause")
            wait_until(
                lambda: (database_row(database, created_id) or {}).get("status") == "paused",
                timeout=8,
                detail="tab pause",
            )
            page_two.reload(wait_until="domcontentloaded", timeout=15_000)
            page_two.locator("#terminal-xterm .xterm").wait_for(
                state="visible", timeout=15_000
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=15_000,
            )
            click_action(page_two, "Resume")
            wait_until(
                lambda: (database_row(database, created_id) or {}).get("status") == "running",
                timeout=8,
                detail="tab resume",
            )
            hidden = database_row(database, created_id)
            if hidden is None:
                raise AssertionError("browser-created tab vanished before hide")
            hidden_tmux_name = str(hidden["tmux_name"])
            click_action(page_two, "Hide tab")
            wait_until(
                lambda: (
                    (row := database_row(database, created_id)) is not None
                    and row.get("status") == "running"
                    and row.get("visible") == 0
                ),
                timeout=12,
                detail="tab hide",
            )
            page_two.wait_for_function(
                "id => ![...document.querySelectorAll('.workspace-tab')].some(tab => tab.dataset.paneId === id)",
                arg=created_id,
                timeout=12_000,
            )
            if subprocess.run(
                [str(tmux), "has-session", "-t", hidden_tmux_name],
                env=base_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode != 0:
                raise AssertionError("hiding the browser tab killed its tmux process")

            kill_command = shlex.join(
                [
                    str(args.python.resolve()),
                    "-u",
                    str(fixture),
                    str(kill_raw),
                    str(kill_events),
                    "KILL",
                ]
            )
            page_two.get_by_role("button", name="New tab", exact=True).click()
            page_two.locator('dialog input[name="cwd"]').fill(str(run_root))
            page_two.locator('dialog input[name="name"]').fill("Kill Target")
            page_two.locator('dialog input[name="command"]').fill(kill_command)
            page_two.get_by_role("button", name="Create tab", exact=True).click()
            page_two.locator(".workspace-tab-title", has_text="Kill Target").wait_for(
                state="visible", timeout=20_000
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                timeout=20_000,
            )
            page_two.wait_for_function(
                "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('CODEX COMPOSER › KILL')",
                timeout=15_000,
            )
            kill_id = urllib.parse.unquote(
                urllib.parse.urlparse(page_two.url).path.rsplit("/", 1)[-1]
            )
            kill_row = database_row(database, kill_id)
            if not kill_row or kill_row["status"] != "running":
                raise AssertionError(f"kill target was not running: {kill_row!r}")
            kill_tmux_name = str(kill_row["tmux_name"])
            page_two.once("dialog", lambda dialog: dialog.accept())
            click_action(page_two, "Kill process…")
            wait_until(
                lambda: (
                    (row := database_row(database, kill_id)) is not None
                    and row.get("status") == "stopped"
                    and row.get("visible") == 0
                ),
                timeout=12,
                detail="explicit process kill",
            )
            page_two.wait_for_function(
                "id => ![...document.querySelectorAll('.workspace-tab')].some(tab => tab.dataset.paneId === id)",
                arg=kill_id,
                timeout=12_000,
            )
            if subprocess.run(
                [str(tmux), "has-session", "-t", kill_tmux_name],
                env=base_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode == 0:
                raise AssertionError("explicit Kill process left the tmux process alive")
            result["lifecycle"].update(
                {
                    "new": True,
                    "rename": True,
                    "pin": True,
                    "unpin": True,
                    "pause": True,
                    "resume": True,
                    "hide_without_kill": True,
                    "kill_process": True,
                    "kill_id": kill_id,
                }
            )
            result["assertions"]["complete_tab_lifecycle"] = True

            screenshot = output / "terminal-browser-proof.png"
            page_two.screenshot(path=str(screenshot), full_page=True)
            result["screenshot"] = {
                "path": str(screenshot),
                "bytes": screenshot.stat().st_size,
                "sha256": sha256_file(screenshot),
            }
            context_one.close()
            context_two.close()
            browser.close()

        errors = result["page_errors"]
        failures = [
            item
            for item in result["request_failures"]
            if "/_termdeck/api/v1/windows/random" not in item.get("url", "")
        ]
        if errors:
            raise AssertionError(f"browser page errors: {errors!r}")
        if failures:
            raise AssertionError(f"unexpected browser request failures: {failures!r}")
        result["assertions"]["no_browser_errors"] = True
        if not all(result["assertions"].values()):
            raise AssertionError(f"one or more browser assertions are false: {result['assertions']!r}")

        shutil.copy2(initial_raw, output / "initial-input.raw")
        shutil.copy2(initial_events, output / "initial-events.txt")
        if created_raw.exists():
            shutil.copy2(created_raw, output / "created-input.raw")
        if created_events.exists():
            shutil.copy2(created_events, output / "created-events.txt")
        if kill_raw.exists():
            shutil.copy2(kill_raw, output / "kill-input.raw")
        if kill_events.exists():
            shutil.copy2(kill_events, output / "kill-events.txt")
        result["ok"] = True
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if "result" in locals():
            (output / "partial-result.json").write_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        for source, name in (
            (initial_raw, "initial-input.raw"),
            (initial_events, "initial-events.txt"),
            (created_raw, "created-input.raw"),
            (created_events, "created-events.txt"),
            (kill_raw, "kill-input.raw"),
            (kill_events, "kill-events.txt"),
        ):
            if source.exists():
                shutil.copy2(source, output / name)
        stop_server(server)
        subprocess.run(
            [str(tmux), "kill-server"],
            env=base_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if args.keep_temp:
            (output / "temporary-root.txt").write_text(str(run_root) + "\n", encoding="utf-8")
        else:
            shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
