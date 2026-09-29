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
  window.__termdeckActiveSocket = () => {
    const pane = document.querySelector('.workspace-tab-select[aria-selected="true"]')?.dataset.paneId
      || document.querySelector('[data-termdeck-pane]')?.dataset.termdeckPane;
    return [...sockets].reverse().find(socket => socket.readyState === WebSocket.OPEN
      && socket.url.endsWith('/panes/' + encodeURIComponent(pane)));
  };
  window.WebSocket = WrappedWebSocket;
})();
"""

ACK_DELAY_INIT = r"""
(() => {
  const nativeAddEventListener = WebSocket.prototype.addEventListener;
  WebSocket.prototype.addEventListener = function(type, listener, options) {
    if (type !== "message" || typeof listener !== "function") {
      return nativeAddEventListener.call(this, type, listener, options);
    }
    const wrapped = function(event) {
      let delay = 0;
      if (typeof event.data === "string") {
        try {
          if (JSON.parse(event.data)?.type === "ack") delay = 120;
        } catch (_ignored) {
          // Non-JSON text frames remain immediate.
        }
      }
      if (delay > 0) {
        setTimeout(() => listener.call(this, event), delay);
      } else {
        listener.call(this, event);
      }
    };
    return nativeAddEventListener.call(this, type, wrapped, options);
  };
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


def percentile(values: list[float], quantile: float) -> float:
    """Return an interpolated percentile for a bounded timing sample."""
    if not values or not 0.0 <= quantile <= 1.0:
        raise ValueError("percentile requires a non-empty sample and a [0, 1] quantile")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


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
            echo_window = bytearray()
            echo_rendered = False
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
                    if not echo_rendered:
                        echo_window.extend(data)
                        if len(echo_window) > 128:
                            del echo_window[:-128]
                        if b"UX_ECHO_REQUEST" in echo_window:
                            os.write(
                                sys.stdout.fileno(),
                                b"\r\nUX_ECHO_RENDERED\r\n",
                            )
                            echo_rendered = True
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
            "SELECT id,name,status,visible,pinned,window_id,window_title,tmux_name,last_pid,tab_index FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def click_action(page: Page, action: str) -> None:
    page.locator('summary[aria-label="Tab actions"]').click()
    page.locator(".workspace-action-panel").get_by_role("button", name=action, exact=True).click()


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
    parser.add_argument("--manager-only", action="store_true", help="run only the disposable manager/settings interaction matrix")
    parser.add_argument("--managed-tmux", action="store_true", help="exercise persistent managed socket routing in disposable storage")
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
    for key in ("TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY"):
        base_env.pop(key, None)
    if args.managed_tmux:
        info = (run_root / "tmux").stat()
        base_env.update({
            "TERMDECK_STATE_ROOT": str(run_root),
            "TERMDECK_TMUX_RUNTIME": str(run_root / "tmux"),
            "TERMDECK_TMUX_IDENTITY": f"{info.st_dev}:{info.st_ino}:{info.st_uid}",
        })

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
                "/usr/bin/tmux" if args.managed_tmux else str(tmux),
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
            "managed_tmux": args.managed_tmux,
        },
        "assertions": {},
        "timings_ms": {},
        "timing_notes": {
            "workspace_ready": "navigation start to the mounted workspace shell and visible terminal host",
            "terminal_ready": "navigation start to the fixture marker in the xterm terminal buffer",
            "keyboard_to_terminal_buffer_echo": (
                "Playwright keyboard dispatch through the fixture and WebSocket to an xterm buffer marker; "
                "does not measure physical display latency"
            ),
            "tab_switch": (
                "Playwright tab click through route, Live status, and fixture marker in the xterm buffer; "
                "includes browser automation overhead"
            ),
        },
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
        # Preserve the caller's qualified temporary filesystem. Forcing /tmp
        # exhausts per-user quota on production hosts before Chromium starts.
        tempfile.tempdir = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(chromium),
                headless=True,
                args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
            )
            auth_context = browser.new_context(viewport={"width": 320, "height": 568})
            auth_page = auth_context.new_page()
            auth_page.goto(f"{origin}/_termdeck/login")
            auth_page.wait_for_url("**/_termdeck/setup")
            if auth_page.locator("input").count() != 2:
                raise AssertionError("initial setup must ask only for password and confirmation")
            auth_page.screenshot(path=str(output / "password-setup-mobile.png"))
            fixture_password = "t1000-disposable-ui-password-123"
            auth_page.get_by_label("Password", exact=True).fill(fixture_password)
            auth_page.get_by_label("Confirm password", exact=True).fill(fixture_password)
            auth_page.get_by_role("button", name="Set password and open t1000").click()
            try:
                auth_page.wait_for_url(f"{origin}/", timeout=10_000)
            except Exception as exc:
                auth_page.screenshot(path=str(output / "password-setup-failure.png"))
                raise AssertionError(f"password setup did not open workspace: {auth_page.url}; {auth_page.locator('body').inner_text()[:800]}") from exc
            auth_context.clear_cookies()
            auth_page.goto(f"{origin}/_termdeck/login")
            if auth_page.locator("input").count() != 1:
                raise AssertionError("sign-in must ask only for the password")
            if not auth_page.get_by_label("Password", exact=True).evaluate("el => el === document.activeElement"):
                raise AssertionError("password field did not receive initial focus")
            if auth_page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                raise AssertionError("password form overflows mobile viewport")
            auth_page.screenshot(path=str(output / "password-login-mobile.png"))
            auth_page.set_viewport_size({"width": 1440, "height": 900})
            auth_page.screenshot(path=str(output / "password-login-desktop.png"))
            auth_page.get_by_label("Password", exact=True).fill("incorrect-fixture-password")
            auth_page.get_by_role("button", name="Sign in", exact=True).click()
            auth_page.locator('#auth-error[role="alert"]').wait_for(state="visible")
            if auth_page.get_by_label("Password", exact=True).get_attribute("aria-invalid") != "true":
                raise AssertionError("password error is not associated with its field")
            auth_page.get_by_label("Password", exact=True).fill(fixture_password)
            auth_page.get_by_role("button", name="Sign in", exact=True).click()
            auth_page.wait_for_url(f"{origin}/")
            auth_context.close()
            result["assertions"]["password_only_accessible_responsive_auth"] = True
            context_one = browser.new_context(viewport={"width": 1440, "height": 900})
            context_two = browser.new_context(viewport={"width": 1280, "height": 800})
            context_one.add_init_script(SOCKET_CAPTURE_INIT)
            context_one.add_init_script(ACK_DELAY_INIT)
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
                def failed_request(request):
                    record = {"page": label, "url": request.url, "failure": str(request.failure)}
                    if request.url.endswith("/_termdeck/api/v1/management"):
                        try:
                            payload = request.post_data_json
                            record["management_operation"] = payload.get("operation") if isinstance(payload, dict) else None
                        except Exception:
                            record["management_operation"] = None
                    result["request_failures"].append(record)
                page.on("requestfailed", failed_request)

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
                page = page_one if label == "one" else page_two
                active_url = page.evaluate("() => window.__termdeckActiveSocket()?.url || ''")
                for record in reversed(result["websockets"][label]):
                    if record["url"] != active_url or record.get("closed"):
                        continue
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
                      const socket = window.__termdeckActiveSocket();
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

            def assert_workspace_layout(
                page: Page,
                *,
                width: int,
                height: int,
                label: str,
            ) -> dict[str, Any]:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(180)
                payload = page.evaluate(
                    r"""() => {
                      const rect = selector => {
                        const element = document.querySelector(selector);
                        if (!element) return null;
                        const value = element.getBoundingClientRect();
                        return {
                          left: value.left,
                          top: value.top,
                          right: value.right,
                          bottom: value.bottom,
                          width: value.width,
                          height: value.height,
                        };
                      };
                      const visible = selector => {
                        const element = document.querySelector(selector);
                        if (!element) return false;
                        const style = getComputedStyle(element);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                      };
                      return {
                        viewport: { width: innerWidth, height: innerHeight },
                        documentScrollWidth: document.documentElement.scrollWidth,
                        documentScrollHeight: document.documentElement.scrollHeight,
                        bodyScrollWidth: document.body.scrollWidth,
                        bodyScrollHeight: document.body.scrollHeight,
                        main: rect('.workspace-main'),
                        header: rect('.workspace-headerbar'),
                        tabbar: rect('.workspace-tabbar'),
                        shell: rect('.workspace-shell'),
                        left: rect('.workspace-header-left'),
                        title: rect('.workspace-title-group'),
                        right: rect('.workspace-header-right'),
                        newTab: rect('.workspace-new-tab'),
                        actions: rect('.workspace-actions summary'),
                        terminal: rect('#terminal-xterm'),
                        statusVisible: visible('.workspace-status'),
                        terminalStatusVisible: visible('.terminal-status'),
                      };
                    }"""
                )
                tolerance = 1.5
                viewport = payload["viewport"]
                violations: list[str] = []
                if payload["documentScrollWidth"] > viewport["width"] + tolerance:
                    violations.append(
                        f"document horizontal overflow {payload['documentScrollWidth']} > {viewport['width']}"
                    )
                if payload["bodyScrollWidth"] > viewport["width"] + tolerance:
                    violations.append(
                        f"body horizontal overflow {payload['bodyScrollWidth']} > {viewport['width']}"
                    )
                if payload["documentScrollHeight"] > viewport["height"] + tolerance:
                    violations.append(
                        f"document vertical overflow {payload['documentScrollHeight']} > {viewport['height']}"
                    )
                for name in ("main", "header", "tabbar", "shell", "terminal"):
                    value = payload.get(name)
                    if not isinstance(value, dict):
                        violations.append(f"{name} is missing")
                        continue
                    if value["left"] < -tolerance or value["top"] < -tolerance:
                        violations.append(f"{name} starts outside viewport: {value}")
                    if value["right"] > viewport["width"] + tolerance:
                        violations.append(f"{name} exceeds viewport width: {value}")
                    if value["bottom"] > viewport["height"] + tolerance:
                        violations.append(f"{name} exceeds viewport height: {value}")
                header = payload.get("header") or {}
                tabbar = payload.get("tabbar") or {}
                shell = payload.get("shell") or {}
                if header and tabbar and header["bottom"] > tabbar["top"] + tolerance:
                    violations.append(f"header overlaps tabbar: header={header}, tabbar={tabbar}")
                if tabbar and shell and tabbar["bottom"] > shell["top"] + tolerance:
                    violations.append(f"tabbar overlaps terminal shell: tabbar={tabbar}, shell={shell}")
                if shell and shell.get("height", 0) < max(180, height * 0.55):
                    violations.append(f"terminal shell is too short: {shell}")
                for name in ("newTab", "actions"):
                    value = payload.get(name)
                    if not isinstance(value, dict) or not header:
                        violations.append(f"{name} is missing")
                    elif (
                        value["left"] < header["left"] - tolerance
                        or value["right"] > header["right"] + tolerance
                        or value["top"] < header["top"] - tolerance
                        or value["bottom"] > header["bottom"] + tolerance
                    ):
                        violations.append(f"{name} escapes header: {value}")
                def overlaps(first: dict[str, float] | None, second: dict[str, float] | None) -> bool:
                    if not first or not second:
                        return False
                    return not (
                        first["right"] <= second["left"] + tolerance
                        or second["right"] <= first["left"] + tolerance
                        or first["bottom"] <= second["top"] + tolerance
                        or second["bottom"] <= first["top"] + tolerance
                    )
                if overlaps(payload.get("left"), payload.get("title")):
                    violations.append(
                        f"left controls overlap title: left={payload.get('left')}, title={payload.get('title')}"
                    )
                if overlaps(payload.get("title"), payload.get("right")):
                    violations.append(
                        f"title overlaps right controls: title={payload.get('title')}, right={payload.get('right')}"
                    )
                if violations:
                    raise AssertionError(f"workspace layout {label} failed: {violations}; payload={payload}")
                return payload

            instrument(page_one, "one")
            instrument(page_two, "two")

            if args.manager_only:
                created = run_json([str(t1000), "new", "--db", str(database), "--cwd", str(run_root),
                                    "--window-id", "win-random", "--window-title", "Random", "--name", "Browser Created",
                                    "--command", shlex.join([str(args.python.resolve()), "-u", str(fixture), str(created_raw), str(created_events), "SECOND"])], env=base_env, timeout=40)
                page_two.goto(f"{origin}/random")
                page_two.wait_for_function("() => document.querySelectorAll('[role=tab]').length === 2")
                from web_management_browser_checks import check_management
                check_management(page_two, origin=origin, database=database,
                                 initial_id=initial_id, created_id=created["tabs"][0]["id"], output=output,
                                 result=result, database_row=database_row,
                                 wait_until=wait_until, click_action=click_action,
                                 t1000=t1000, run_root=run_root, base_env=base_env, run_json=run_json)
                result["scope"] = "manager-settings-only"
                result["assertions"]["no_browser_errors"] = not result["page_errors"]
                result["ok"] = all(result["assertions"].values())
                (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                browser.close()
                print(json.dumps({"ok": result["ok"], "assertions": result["assertions"]}))
                return 0 if result["ok"] else 1

            api_url = f"{origin}/_termdeck/api/v1/windows/random"
            page_one.route(api_url, lambda route: route.abort("failed"))
            started = time.monotonic_ns()
            page_one.goto(f"{origin}/random", wait_until="domcontentloaded", timeout=15_000)
            page_one.wait_for_function(
                """() => {
                  const root = document.querySelector('#termdeck-app');
                  const shell = document.querySelector('.workspace-shell');
                  const host = document.querySelector('#terminal-xterm');
                  if (!root || !shell || !host) {
                    return false;
                  }
                  const shellBox = shell.getBoundingClientRect();
                  const hostBox = host.getBoundingClientRect();
                  return root.classList.contains('workspace-main')
                    && shellBox.width > 0 && shellBox.height > 0
                    && hostBox.width > 0 && hostBox.height > 0;
                }""",
                timeout=10_000,
            )
            workspace_ready_ms = (time.monotonic_ns() - started) / 1_000_000
            result["timings_ms"]["workspace_ready"] = round(workspace_ready_ms, 3)
            try:
                page_one.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=20_000)
            except Exception:
                result["initial_visibility_diagnostic"] = page_one.evaluate(
                    r"""() => {
                      const shell = document.querySelector('.workspace-shell');
                      const host = document.querySelector('#terminal-xterm');
                      const terminal = host?.querySelector('.xterm');
                      const box = element => element ? {
                        width: element.getBoundingClientRect().width,
                        height: element.getBoundingClientRect().height,
                        top: element.getBoundingClientRect().top,
                        bottom: element.getBoundingClientRect().bottom,
                      } : null;
                      const style = element => element ? {
                        display: getComputedStyle(element).display,
                        visibility: getComputedStyle(element).visibility,
                        opacity: getComputedStyle(element).opacity,
                        width: element.getBoundingClientRect().width,
                        height: element.getBoundingClientRect().height,
                      } : null;
                      return {
                        rootState: document.querySelector('#termdeck-app')?.dataset.termdeckState,
                        rootBusy: document.querySelector('#termdeck-app')?.getAttribute('aria-busy'),
                        shellState: shell?.dataset.state,
                        shellStatus: document.querySelector('.workspace-state-message')?.textContent,
                        terminalStatus: document.querySelector('#terminal-status')?.textContent,
                        viewport: { width: innerWidth, height: innerHeight },
                        body: box(document.body),
                        root: box(document.querySelector('#termdeck-app')),
                        shell: box(shell),
                        gridRows: getComputedStyle(document.querySelector('#termdeck-app')).gridTemplateRows,
                        children: [...(document.querySelector('#termdeck-app')?.children || [])].map(element => ({
                          tag: element.tagName,
                          className: element.className,
                          display: getComputedStyle(element).display,
                          rect: box(element),
                        })),
                        host: style(host),
                        terminal: style(terminal),
                        bodyClass: document.body.className,
                      };
                    }"""
                )
                raise
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
                """() => {
                  const shell = document.querySelector('.workspace-shell');
                  const terminal = document.querySelector('#terminal-xterm .xterm');
                  return (shell?.dataset.state === 'ready' || shell?.dataset.state === 'degraded') && terminal
                    && getComputedStyle(terminal).visibility !== 'hidden';
                }""",
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
            terminal_ready_ms = (time.monotonic_ns() - started) / 1_000_000
            result["timings_ms"]["terminal_ready"] = round(terminal_ready_ms, 3)
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

            result["layout"] = {
                "desktop": assert_workspace_layout(page_one, width=1440, height=900, label="desktop"),
                "compact": assert_workspace_layout(page_one, width=980, height=620, label="compact"),
                "mobile": assert_workspace_layout(page_one, width=430, height=740, label="mobile"),
            }
            page_one.set_viewport_size({"width": 1440, "height": 900})
            page_one.wait_for_timeout(180)
            result["assertions"]["responsive_workspace_layout"] = True

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
            terminal_focus_after_tab = page_one.evaluate(
                "() => document.querySelector('#terminal-xterm')?.contains(document.activeElement) === true"
            )
            if not terminal_focus_after_tab:
                raise AssertionError(
                    "terminal lost keyboard focus after Tab: "
                    + str(page_one.evaluate("document.activeElement?.outerHTML.slice(0, 300)"))
                )
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

            render_echo_started = time.monotonic_ns()
            page_one.keyboard.type("UX_ECHO_REQUEST")
            page_one.keyboard.press("Enter")
            try:
                page_one.wait_for_function(
                    "() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('UX_ECHO_RENDERED')",
                    timeout=5_000,
                )
            except Exception as exc:
                diagnostic = {
                    "active_element": page_one.evaluate("document.activeElement?.outerHTML.slice(0, 300)"),
                    "raw_tail_hex": initial_raw.read_bytes()[-120:].hex(),
                    "terminal_tail": page_one.evaluate(
                        "document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer()?.slice(-500)"
                    ),
                }
                (output / "echo-timeout-diagnostic.json").write_text(
                    json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8"
                )
                raise AssertionError(f"terminal echo timed out: {diagnostic}") from exc
            render_echo_ms = (time.monotonic_ns() - render_echo_started) / 1_000_000
            result["timings_ms"]["keyboard_to_terminal_buffer_echo"] = round(render_echo_ms, 3)
            result["assertions"]["keyboard_to_terminal_buffer_echo"] = True
            if not page_one.evaluate(
                "() => document.querySelector('#terminal-xterm')?.contains(document.activeElement) === true"
            ):
                raise AssertionError(
                    "terminal lost keyboard focus after output: "
                    + str(page_one.evaluate("document.activeElement?.outerHTML.slice(0, 300)"))
                )

            latency_text = "PIPELINED_" + ("abcdefghijklmnopqrstuvwxyz0123456789" * 5)
            latency_offset = len(initial_raw.read_bytes())
            latency_started = time.monotonic_ns()
            page_one.keyboard.type(latency_text)
            latency_dispatched = time.monotonic_ns()
            try:
                wait_until(
                    lambda: latency_text.encode("utf-8") in initial_raw.read_bytes()[latency_offset:],
                    timeout=5,
                    detail="120ms-ack delayed typing burst",
                )
            except Exception as exc:
                diagnostic = {
                    "active_element": page_one.evaluate("document.activeElement?.outerHTML.slice(0, 300)"),
                    "raw_tail_hex": initial_raw.read_bytes()[-240:].hex(),
                }
                (output / "typing-timeout-diagnostic.json").write_text(
                    json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8"
                )
                raise AssertionError(f"typing burst timed out: {diagnostic}") from exc
            latency_finished = time.monotonic_ns()
            latency_total_ms = (latency_finished - latency_started) / 1_000_000
            latency_after_dispatch_ms = (latency_finished - latency_dispatched) / 1_000_000
            if latency_total_ms > 2000 or latency_after_dispatch_ms > 1500:
                raise AssertionError(
                    f"pipelined input exceeded latency budget: total={latency_total_ms:.1f} ms, "
                    f"after-dispatch={latency_after_dispatch_ms:.1f} ms"
                )
            result["timings_ms"].update(
                {
                    "delayed_ack_ms": 120,
                    "typing_burst_bytes": len(latency_text.encode("utf-8")),
                    "typing_dispatch": round((latency_dispatched - latency_started) / 1_000_000, 3),
                    "typing_visible_total": round(latency_total_ms, 3),
                    "typing_visible_after_dispatch": round(latency_after_dispatch_ms, 3),
                }
            )
            result["assertions"]["bounded_pipelined_input_latency"] = True

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
            state_before_resize = terminal_state(page_one)
            page_one.set_viewport_size({"width": 980, "height": 620})
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) != geometry_before,
                timeout=8,
                detail="active browser shrinks only its tmux window",
            )
            geometry_small = tmux_geometry(tmux, base_env, initial_tmux_name)
            state_small = terminal_state(page_one)
            page_one.set_viewport_size({"width": 1600, "height": 1000})
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) != geometry_small,
                timeout=8,
                detail="active browser grows only its tmux window",
            )
            geometry_large = tmux_geometry(tmux, base_env, initial_tmux_name)
            state_large = terminal_state(page_one)
            before_size = tuple(int(part) for part in geometry_before.split("x", 1))
            small_size = tuple(int(part) for part in geometry_small.split("x", 1))
            large_size = tuple(int(part) for part in geometry_large.split("x", 1))
            if not (small_size[0] < before_size[0] < large_size[0]
                    and small_size[1] < before_size[1] < large_size[1]):
                raise AssertionError(
                    f"active browser did not resize its tmux grid with viewport: "
                    f"initial={geometry_before}, small={geometry_small}, large={geometry_large}"
                )
            for label, observed, expected in (("initial", state_before_resize, before_size),
                                              ("small", state_small, small_size),
                                              ("large", state_large, large_size)):
                if (observed.get("cols"), observed.get("rows")) != expected:
                    raise AssertionError(
                        f"browser {label} xterm grid did not match its tmux pane: "
                        f"expected={expected}, observed={(observed.get('cols'), observed.get('rows'))}"
                    )
            viewport_fit = page_one.evaluate(
                """() => {
                  const host = document.querySelector('#terminal-xterm');
                  return {
                    fontSize: Number(host?.dataset.termdeckFontSize || 0),
                    overflow: host?.dataset.termdeckOverflow,
                    columns: Number(host?.dataset.termdeckColumns || 0),
                    rows: Number(host?.dataset.termdeckRows || 0),
                  };
                }"""
            )
            result["geometry"] = {
                "initial": geometry_before,
                "after_small_viewport": geometry_small,
                "after_large_viewport": geometry_large,
                "viewport_fit": viewport_fit,
            }
            result["assertions"]["active_browser_resizes_own_tmux_window"] = True

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
            if second_before.get("mine") is not False:
                raise AssertionError(
                    f"background browser unexpectedly stole terminal control: {second_before!r}"
                )
            if int(second_before.get("epoch") or 0) != int(first_view.get("epoch") or 0):
                raise AssertionError(
                    f"passive second-browser open changed controller epoch: "
                    f"first={first_view!r}, second={second_before!r}"
                )
            # A passive viewer can queue bytes while its focus claim is in
            # flight. They must not be sent with an unowned input sequence.
            passive_marker = b"QUEUED_UNTIL_CONTROL"
            page_two.evaluate(
                "text => document.querySelector('#terminal-xterm').termdeckTerminal.paste(text)",
                passive_marker.decode("ascii"),
            )
            page_two.wait_for_timeout(150)
            if passive_marker in initial_raw.read_bytes():
                raise AssertionError("passive browser sent terminal input before control ownership")
            if (latest_socket_control("two") or {}).get("mine") is not False:
                raise AssertionError("passive terminal input took control without a focus request")
            if tmux_geometry(tmux, base_env, initial_tmux_name) != geometry_large:
                raise AssertionError("passive browser changed the active writer's tmux geometry")
            page_two.set_viewport_size({"width": 980, "height": 620})
            page_two.wait_for_timeout(250)
            if tmux_geometry(tmux, base_env, initial_tmux_name) != geometry_large:
                raise AssertionError("passive browser resize changed the active writer's tmux geometry")
            result["assertions"]["passive_browser_resize_is_size_neutral"] = True
            box_two = page_two.locator("#terminal-xterm").bounding_box()
            if not box_two:
                raise AssertionError("second terminal has no browser geometry")
            page_two.mouse.click(box_two["x"] + 100, box_two["y"] + 100)
            wait_browser_condition(
                page_two,
                lambda: (latest_socket_control("two") or {}).get("mine") is True,
                timeout=5,
                detail="explicit second-browser focus takeover",
            )
            wait_bytes(initial_raw, passive_marker)
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) == geometry_small,
                timeout=8,
                detail="new browser writer applies its own viewport geometry",
            )
            result["assertions"]["focus_takeover_applies_new_writer_size"] = True
            second_focused = latest_socket_control("two") or {}
            if int(second_focused.get("epoch") or 0) <= int(first_view.get("epoch") or 0):
                raise AssertionError(
                    f"explicit second-browser focus did not advance controller epoch: "
                    f"first={first_view!r}, focused={second_focused!r}"
                )
            page_two.wait_for_function(
                "() => document.activeElement?.classList?.contains('xterm-helper-textarea') === true",
                timeout=5_000,
            )
            page_two.keyboard.type("TWO_TAKEOVER")
            page_two.keyboard.press("Enter")
            try:
                wait_bytes(initial_raw, b"TWO_TAKEOVER\r")
            except AssertionError as error:
                raise AssertionError(
                    f"second-browser input did not reach its disposable pane: "
                    f"control={latest_socket_control('two')!r}, "
                    f"focused={page_two.evaluate('document.activeElement?.outerHTML.slice(0, 300)')!r}, "
                    f"terminal={terminal_state(page_two)!r}, "
                    f"raw_tail={initial_raw.read_bytes()[-180:]!r}"
                ) from error
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
                "second_passive_epoch": second_before.get("epoch"),
                "second_focus_takeover_epoch": second_focused.get("epoch"),
                "second_input_epoch": second_after.get("epoch"),
                "passive_browser_does_not_steal": True,
                "explicit_focus_takeover": True,
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
            page_two.wait_for_function(
                "() => document.activeElement?.classList?.contains('xterm-helper-textarea') === true",
                timeout=3_000,
            )
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
            csrf = next(cookie["value"] for cookie in page_two.context.cookies() if cookie["name"] == "termdeck_csrf")
            create_response = page_two.context.request.post(
                f"{origin}/_termdeck/api/v1/windows/random/tabs",
                data=json.dumps({"cwd": str(run_root), "name": "Browser Created", "command": created_command}),
                headers={"X-TermDeck-CSRF": csrf, "Content-Type": "application/json", "Origin": origin,
                         "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
                timeout=40_000,
            )
            if create_response.status != 201:
                raise AssertionError(f"configured tab API failed: {create_response.status}")
            configured_id = create_response.json()["payload"]["tabs"][0]["id"]
            page_two.locator(f'.workspace-tab[data-pane-id="{configured_id}"]').wait_for(state="visible", timeout=20_000)
            page_two.goto(f"{origin}/random/t/{urllib.parse.quote(configured_id)}")
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

            # Keyboard tab navigation must retain focus after asynchronous
            # terminal readiness, while pointer selection must remain click-to-type.
            page_two.locator(f'[role="tab"][data-pane-id="{created_id}"]').focus()
            for key, pane_id in (("Home", initial_id), ("End", created_id)):
                page_two.keyboard.press(key)
                page_two.wait_for_url(f"**/random/t/{urllib.parse.quote(pane_id)}")
                page_two.wait_for_function(
                    "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                    timeout=10_000,
                )
                page_two.evaluate(
                    "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                if not page_two.evaluate(
                    "id => document.activeElement?.getAttribute('role') === 'tab' && document.activeElement.dataset.paneId === id",
                    pane_id,
                ):
                    raise AssertionError(f"terminal readiness stole keyboard tab focus after {key}")
            result["assertions"]["keyboard_tab_focus_survives_terminal_ready"] = True

            page_two.locator(".workspace-actions summary").click()
            page_two.locator(".workspace-action-panel button").first.focus()
            page_two.evaluate("window.__termdeckFocusedAction = document.activeElement")
            with page_two.expect_response(
                lambda response: response.url.endswith("/_termdeck/api/v1/windows/random"),
                timeout=10_000,
            ):
                page_two.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page_two.wait_for_function(
                "() => document.querySelector('#termdeck-app')?.getAttribute('aria-busy') === 'false'",
                timeout=5_000,
            )
            if not page_two.evaluate(
                "() => document.querySelector('.workspace-actions').open && document.activeElement === window.__termdeckFocusedAction && document.activeElement.isConnected"
            ):
                focus_state = page_two.evaluate("""() => ({
                    open: document.querySelector('.workspace-actions').open,
                    active: document.activeElement.outerHTML.slice(0, 500),
                    previous: window.__termdeckFocusedAction.outerHTML,
                    connected: window.__termdeckFocusedAction.isConnected,
                    status: document.querySelector('.workspace-status').textContent,
                })""")
                raise AssertionError(f"metadata refresh replaced or defocused the open More menu: {focus_state}")
            # Force a real metadata change through the disposable CLI while its
            # action remains focused, not merely an unchanged cache refresh.
            page_two.locator(".workspace-action-panel").get_by_role("button", name="Pin", exact=True).focus()
            page_two.evaluate("window.__termdeckFocusedAction = document.activeElement")
            for command, label in (("pin", "Unpin"), ("unpin", "Pin")):
                run_json([str(t1000), command, "--db", str(database), created_id], env=base_env)
                page_two.locator(".workspace-action-panel").get_by_role("button", name=label, exact=True).wait_for()
                if not page_two.evaluate(
                    "() => document.activeElement === window.__termdeckFocusedAction && document.activeElement.isConnected"
                ):
                    raise AssertionError("changed tab metadata replaced the focused action control")
            result["assertions"]["refresh_preserves_open_menu_focus"] = True
            page_two.keyboard.press("Escape")

            def switch_pane_timed(pane_id: str, marker: str) -> float:
                selector = f'.workspace-tab[data-pane-id="{pane_id}"] .workspace-tab-select'
                page_two.locator(selector).wait_for(state="visible", timeout=10_000)
                switch_started = time.monotonic_ns()
                page_two.locator(selector).click()
                page_two.wait_for_url(
                    f"**/random/t/{urllib.parse.quote(pane_id)}",
                    timeout=10_000,
                )
                page_two.wait_for_function(
                    "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
                    timeout=10_000,
                )
                page_two.wait_for_function(
                    "marker => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes(marker)",
                    arg=marker,
                    timeout=10_000,
                )
                page_two.wait_for_function(
                    "() => document.querySelector('#terminal-xterm')?.contains(document.activeElement)",
                    timeout=5_000,
                )
                return (time.monotonic_ns() - switch_started) / 1_000_000

            tab_switch_samples: list[float] = []
            for _sample in range(4):
                tab_switch_samples.append(
                    switch_pane_timed(initial_id, "CODEX COMPOSER › PRIMARY")
                )
                tab_switch_samples.append(
                    switch_pane_timed(created_id, "CODEX COMPOSER › CREATED")
                )
            result["timings_ms"].update(
                {
                    "tab_switch_count": len(tab_switch_samples),
                    "tab_switch_samples": [round(sample, 3) for sample in tab_switch_samples],
                    "tab_switch_p50": round(percentile(tab_switch_samples, 0.50), 3),
                    "tab_switch_p95": round(percentile(tab_switch_samples, 0.95), 3),
                }
            )
            result["assertions"]["repeated_tab_switch_latency"] = True
            result["assertions"]["pointer_tab_switch_preserves_typing_focus"] = True

            # Desktop-parity tools must operate on the real terminal, not just
            # open decorative panels. Shortcuts must never leak into the PTY.
            before_tools = initial_raw.read_bytes()
            click_action(page_two, "Show/hide Tabs pane")
            sidebar = page_two.get_by_role("complementary", name="Tabs pane")
            sidebar.wait_for(state="visible")
            sidebar.get_by_role("searchbox").fill("no-such-sidebar-tab")
            sidebar.get_by_text("No matching tabs", exact=True).wait_for()
            sidebar.get_by_role("searchbox").fill("")
            sidebar.locator(f'button[data-pane-id="{initial_id}"]').click()
            page_two.wait_for_url(f"**/random/t/{urllib.parse.quote(initial_id)}")
            page_two.wait_for_function("() => document.querySelector('#terminal-status')?.textContent === 'Live'")
            page_two.wait_for_function("() => document.querySelector('#terminal-xterm')?.contains(document.activeElement)")
            sidebar.get_by_role("button", name="Move Tabs pane to left", exact=True).click()
            page_two.wait_for_function("() => document.querySelector('.workspace-shell').dataset.sidebarSide === 'left'")
            sidebar.get_by_role("button", name="Rename", exact=True).click()
            page_two.locator("#workspace-action-dialog").get_by_role("button", name="Cancel", exact=True).click()
            page_two.screenshot(path=str(output / "desktop-style-tabs-pane.png"))
            page_two.reload(wait_until="domcontentloaded")
            page_two.wait_for_function("() => document.querySelector('#terminal-status')?.textContent === 'Live'")
            sidebar.wait_for(state="visible")
            if page_two.locator(".workspace-shell").get_attribute("data-sidebar-side") != "left":
                raise AssertionError("Tabs pane position did not survive reload")
            page_two.set_viewport_size({"width": 320, "height": 568})
            sidebar.get_by_role("button", name="Move Tabs pane to right", exact=True).click()
            bounds = sidebar.bounding_box()
            if not bounds or bounds["x"] < 0 or bounds["x"] + bounds["width"] > 321:
                raise AssertionError("mobile Tabs pane overflows the viewport")
            page_two.screenshot(path=str(output / "desktop-style-tabs-pane-mobile.png"))
            sidebar.get_by_role("button", name="Hide Tabs pane", exact=True).click()
            sidebar.wait_for(state="hidden")
            mobile_geometry = tmux_geometry(tmux, base_env, initial_tmux_name)
            control_before_desktop = latest_socket_control("two")
            page_two.set_viewport_size({"width": 1280, "height": 800})
            try:
                wait_until(
                    lambda: tmux_geometry(tmux, base_env, initial_tmux_name) != mobile_geometry,
                    timeout=8,
                    detail="desktop terminal grows after leaving mobile Tabs pane layout",
                )
            except AssertionError as error:
                debug = page_two.evaluate("""() => {
                  const host = document.querySelector('#terminal-xterm');
                  const api = host?.termdeckTerminal;
                  return {hostWidth: host?.clientWidth, hostHeight: host?.clientHeight,
                    cols: api?.terminal?.cols, rows: api?.terminal?.rows,
                    focused: document.activeElement === api?.terminal?.textarea,
                    status: document.querySelector('#terminal-status')?.textContent};
                }""")
                raise AssertionError(
                    f"{error}; mobile={mobile_geometry}, now={tmux_geometry(tmux, base_env, initial_tmux_name)}, "
                    f"control_before={control_before_desktop}, control_now={latest_socket_control('two')}, browser={debug}"
                ) from error
            desktop_geometry = tmux_geometry(tmux, base_env, initial_tmux_name)
            mobile_size = tuple(int(part) for part in mobile_geometry.split("x", 1))
            desktop_size = tuple(int(part) for part in desktop_geometry.split("x", 1))
            if not (desktop_size[0] > mobile_size[0] and desktop_size[1] > mobile_size[1]):
                raise AssertionError(f"terminal did not grow with workspace: {mobile_geometry} -> {desktop_geometry}")
            page_two.wait_for_function(
                """expected => {
                  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
                  return terminal?.cols === expected[0] && terminal?.rows === expected[1];
                }""",
                arg=list(desktop_size),
                timeout=8_000,
            )
            result["geometry"]["after_tabs_pane_mobile"] = mobile_geometry
            result["geometry"]["after_tabs_pane_desktop"] = desktop_geometry
            result["assertions"]["desktop_tabs_pane_navigation_layout_and_persistence"] = True
            page_two.keyboard.press("Control+Shift+k")
            tools_dialog = page_two.locator("#workspace-tools-dialog")
            tools_dialog.wait_for(state="visible")
            page_two.keyboard.press("ArrowUp")
            if not tools_dialog.locator(".workspace-picker-results button").last.evaluate("el => el === document.activeElement"):
                raise AssertionError("Up Arrow from the search field did not select the last tab")
            page_two.keyboard.press("ArrowDown")
            if not tools_dialog.locator(".workspace-picker-results button").first.evaluate("el => el === document.activeElement"):
                raise AssertionError("Down Arrow did not wrap from the last tab to the first")
            tools_dialog.get_by_role("searchbox").fill("no-such-t1000-tab")
            tools_dialog.get_by_text("No matches", exact=True).wait_for()
            tools_dialog.get_by_role("searchbox").fill("PRIMARY")
            # The primary fixture's title is read from the registry, not its
            # terminal output, so search by the actual session title.
            tools_dialog.get_by_role("searchbox").fill(str(database_row(database, initial_id)["name"]))
            page_two.keyboard.press("Enter")
            page_two.wait_for_url(f"**/random/t/{urllib.parse.quote(initial_id)}")
            page_two.wait_for_function("() => document.querySelector('#terminal-status')?.textContent === 'Live'")
            page_two.wait_for_function("() => document.querySelector('#terminal-xterm')?.contains(document.activeElement)")
            page_two.keyboard.press("Control+Shift+f")
            tools_dialog.get_by_role("searchbox").fill("UNICODE_OK")
            tools_dialog.get_by_text("Match selected in terminal", exact=True).wait_for()
            if not page_two.evaluate("() => document.querySelector('#terminal-xterm').termdeckTerminal.selection().includes('UNICODE_OK')"):
                raise AssertionError("Find in terminal did not select the real terminal text")
            tools_dialog.get_by_role("searchbox").fill("no-such-terminal-text")
            tools_dialog.get_by_text("No matches", exact=True).wait_for()
            page_two.keyboard.press("Escape")
            page_two.keyboard.press("Control+Shift+p")
            tools_dialog.get_by_role("textbox", name="Codex prompt").wait_for(timeout=3000)
            tools_dialog.get_by_role("combobox", name="Directory").wait_for(timeout=3000)
            page_two.keyboard.press("Escape")
            click_action(page_two, "Settings · themes and fonts")
            try:
                tools_dialog.get_by_label("Terminal text size (px)").wait_for(timeout=3000)
            except Exception as exc:
                page_two.screenshot(path=str(output / "palette-transition-failure.png"))
                raise AssertionError(f"appearance dialog failed: {tools_dialog.evaluate('el => el.outerHTML')}; errors={result['page_errors']}") from exc
            tools_dialog.get_by_label("Terminal text size (px)").fill("16")
            tools_dialog.get_by_label("Terminal text size (px)").press("Tab")
            tools_dialog.get_by_label("Cursor shape").select_option("bar")
            tools_dialog.get_by_label("Blink cursor").uncheck()
            tools_dialog.get_by_label("Terminal palette").select_option("nord")
            tools_dialog.get_by_label("Terminal font", exact=True).select_option("liberation")
            wait_until(
                lambda: tmux_geometry(tmux, base_env, initial_tmux_name) != desktop_geometry,
                timeout=8,
                detail="selected terminal font changes the active writer's tmux grid",
            )
            appearance_geometry = tmux_geometry(tmux, base_env, initial_tmux_name)
            page_two.screenshot(path=str(output / "terminal-appearance.png"))
            page_two.keyboard.press("Escape")
            page_two.reload(wait_until="domcontentloaded")
            page_two.wait_for_function("() => document.querySelector('#terminal-status')?.textContent === 'Live'")
            options = page_two.evaluate("""() => {
                const terminal = document.querySelector('#terminal-xterm').termdeckTerminal.terminal;
                return {cursor: terminal.options.cursorStyle, blink: terminal.options.cursorBlink, cols: terminal.cols, rows: terminal.rows,
                  background: terminal.options.theme.background, red: terminal.options.theme.red, font: terminal.options.fontFamily};
            }""")
            current_size = tuple(int(part) for part in tmux_geometry(tmux, base_env, initial_tmux_name).split("x", 1))
            if options != {"cursor": "bar", "blink": False, "cols": current_size[0], "rows": current_size[1],
                           "background": "#2e3440", "red": "#bf616a", "font": '"Liberation Mono", monospace'}:
                raise AssertionError(f"appearance did not persist or xterm diverged from tmux {current_size}: {options}")
            result["geometry"]["after_appearance"] = appearance_geometry
            click_action(page_two, "Settings · themes and fonts")
            palette_backgrounds = {"t1000": "#0a0d13", "terminator": "#000000", "catppuccin": "#1e1e2e",
                                   "tokyo-night": "#1a1b26", "nord": "#2e3440", "one-dark": "#282c34", "solarized-dark": "#002b36"}
            for palette, background in palette_backgrounds.items():
                tools_dialog.get_by_label("Terminal palette").select_option(palette)
                page_two.wait_for_function(
                    "bg => document.querySelector('#terminal-xterm').termdeckTerminal.terminal.options.theme.background === bg",
                    arg=background,
                )
            tools_dialog.get_by_role("button", name="Reset to defaults").click()
            if tools_dialog.get_by_label("Terminal palette").input_value() != "t1000" or tools_dialog.get_by_label("Terminal font", exact=True).input_value() != "auto":
                raise AssertionError("terminal palette or font did not reset")
            result["assertions"]["desktop_palette_and_font_preferences_preserve_geometry"] = True
            page_two.keyboard.press("Escape")
            click_action(page_two, "Keyboard shortcuts")
            tools_dialog.get_by_text("Ctrl + Alt + T", exact=True).wait_for()
            page_two.keyboard.press("Escape")
            if initial_raw.read_bytes() != before_tools:
                raise AssertionError("workspace tool shortcuts leaked input into the terminal")
            switch_pane_timed(created_id, "CODEX COMPOSER › CREATED")
            result["assertions"]["codex_palette_find_appearance_and_shortcuts"] = True

            home_page = context_two.new_page()
            home_page.goto(f"{origin}/")
            home_page.locator(".window-list-row").first.wait_for()
            home_page.get_by_role("searchbox").fill("no-such-window")
            home_page.get_by_text("No matching windows or tabs.", exact=True).wait_for()
            home_page.get_by_role("searchbox").fill("random")
            home_page.locator('.window-list-row[href="/random"]').wait_for()
            with home_page.expect_response(lambda response: response.url.endswith("/_termdeck/api/v1/snapshot")):
                home_page.wait_for_timeout(2200)
            if not home_page.get_by_role("searchbox").evaluate("el => el === document.activeElement && el.value === 'random'"):
                raise AssertionError("window refresh lost search or focus")
            home_page.screenshot(path=str(output / "window-list.png"))
            home_page.close()
            result["assertions"]["searchable_window_list_preserves_focus"] = True

            click_action(page_two, "Rename tab")
            action_dialog = page_two.locator("#workspace-action-dialog")
            action_dialog.wait_for(state="visible")
            action_dialog.screenshot(path=str(output / "rename-dialog.png"))
            action_dialog.locator('input[name="rename"]').fill("Renamed Browser Tab")
            action_dialog.get_by_role("button", name="Save name", exact=True).click()
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

            hidden_pid = subprocess.check_output(
                [str(tmux), "display-message", "-p", "-t", hidden_tmux_name, "#{pane_pid}"],
                env=base_env, text=True, timeout=5,
            ).strip()
            rejected_restore = page_two.request.post(
                f"{origin}/_termdeck/api/v1/windows/random/hidden-tabs/{created_id}/restore", data={}
            )
            if rejected_restore.status != 403 or database_row(database, created_id)["visible"] != 0:
                raise AssertionError("hidden-tab restoration bypassed CSRF protection")
            click_action(page_two, "Hidden tabs")
            tools_dialog.get_by_role("button", name="Renamed Browser Tab", exact=False).click()
            tools_dialog.wait_for(state="hidden", timeout=20000)
            page_two.wait_for_url(f"**/random/t/{created_id}")
            page_two.wait_for_function("() => document.querySelector('#terminal-status')?.textContent === 'Live'")
            page_two.wait_for_function("() => document.querySelector('#terminal-xterm')?.contains(document.activeElement)")
            restored_pid = subprocess.check_output(
                [str(tmux), "display-message", "-p", "-t", hidden_tmux_name, "#{pane_pid}"],
                env=base_env, text=True, timeout=5,
            ).strip()
            if restored_pid != hidden_pid or database_row(database, created_id)["visible"] != 1:
                raise AssertionError(f"reopening a hidden tab restarted or failed to restore it: before_pid={hidden_pid}, after_pid={restored_pid}, row={database_row(database, created_id)}")
            click_action(page_two, "Hide tab")
            page_two.wait_for_function(
                "id => ![...document.querySelectorAll('.workspace-tab')].some(tab => tab.dataset.paneId === id)",
                arg=created_id,
            )
            result["assertions"]["hidden_tab_restore_preserves_process_and_csrf"] = True

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
            create_response = page_two.context.request.post(
                f"{origin}/_termdeck/api/v1/windows/random/tabs",
                data=json.dumps({"cwd": str(run_root), "name": "Kill Target", "command": kill_command}),
                headers={"X-TermDeck-CSRF": csrf, "Content-Type": "application/json", "Origin": origin,
                         "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
                timeout=40_000,
            )
            if create_response.status != 201:
                raise AssertionError(f"kill-target API failed: {create_response.status}")
            kill_configured_id = create_response.json()["payload"]["tabs"][0]["id"]
            page_two.locator(f'.workspace-tab[data-pane-id="{kill_configured_id}"]').wait_for(state="visible", timeout=20_000)
            page_two.goto(f"{origin}/random/t/{urllib.parse.quote(kill_configured_id)}")
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
            click_action(page_two, "Kill process…")
            action_dialog.wait_for(state="visible")
            if not action_dialog.get_by_role("button", name="Cancel", exact=True).evaluate("el => el === document.activeElement"):
                raise AssertionError("destructive dialog did not default focus to Cancel")
            action_dialog.screenshot(path=str(output / "kill-dialog.png"))
            for _ in range(5):
                page_two.keyboard.press("Tab")
                if not action_dialog.evaluate("el => el.contains(document.activeElement)"):
                    raise AssertionError("keyboard focus escaped the modal")
            page_two.set_viewport_size({"width": 320, "height": 568})
            box = action_dialog.bounding_box()
            if not box or box["x"] < 0 or box["x"] + box["width"] > 320 or box["height"] > 568:
                raise AssertionError(f"destructive dialog escaped mobile viewport: {box}")
            page_two.screenshot(path=str(output / "kill-dialog-mobile.png"))
            page_two.keyboard.press("Escape")
            action_dialog.wait_for(state="hidden")
            page_two.set_viewport_size({"width": 1280, "height": 800})
            if (database_row(database, kill_id) or {}).get("status") != "running":
                raise AssertionError("cancelling Kill changed the terminal")

            # An action failure must remain visible after terminal reconnects
            # and metadata refreshes, and must not optimistically remove a tab.
            action_url = f"{origin}/_termdeck/api/v1/windows/random/panes/{kill_id}/tab"
            page_two.route(action_url, lambda route: route.fulfill(
                status=409, content_type="application/json",
                body='{"error":"tab-action-failed"}',
            ))
            click_action(page_two, "Kill process…")
            action_dialog.get_by_role("button", name="Kill process", exact=True).click()
            action_notice = page_two.locator(".workspace-action-notice")
            action_notice.wait_for(state="visible")
            if "launcher may need repair" not in action_notice.inner_text():
                raise AssertionError("tab action error did not explain recovery")
            page_two.evaluate("window.dispatchEvent(new Event('online'))")
            with page_two.expect_response(api_url, timeout=10_000):
                page_two.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            if not action_notice.is_visible():
                raise AssertionError("terminal refresh erased the tab action error")
            page_two.screenshot(path=str(output / "action-error.png"))
            if (database_row(database, kill_id) or {}).get("status") != "running":
                raise AssertionError("rejected Kill changed the terminal")
            action_notice.get_by_role("button", name="Dismiss").click()
            page_two.unroute(action_url)
            result["assertions"]["destructive_dialog_cancel_and_persistent_errors"] = True

            click_action(page_two, "Kill process…")
            action_dialog.get_by_role("button", name="Kill process", exact=True).click()
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

            from web_management_browser_checks import check_management
            check_management(page_two, origin=origin, database=database,
                             initial_id=initial_id, created_id=created_id, output=output,
                             result=result, database_row=database_row,
                             wait_until=wait_until, click_action=click_action,
                             t1000=t1000, run_root=run_root, base_env=base_env, run_json=run_json)

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
        from web_management_browser_checks import is_cancelled_manager_read
        result["cancelled_manager_reads"] = [item for item in result["request_failures"] if is_cancelled_manager_read(item)]
        failures = [
            item
            for item in result["request_failures"]
            if "/_termdeck/api/v1/windows/random" not in item.get("url", "")
            and not is_cancelled_manager_read(item)
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
