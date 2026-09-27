#!/usr/bin/env python3
"""Disposable Chromium proof for the full TermDeck browser terminal contract.

The harness never connects to the default tmux server or the production registry. It
creates a unique tmux socket, temporary SQLite/auth stores, random loopback listener,
separate browser profiles and a fixture-only t1000 executable.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
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
from typing import Any, Callable

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
REJECTED_UI = (
    "Take control",
    "Take over control",
    "Release control",
    "Send text",
    "Controller active",
    "typing switches control here",
)
SOCKET_CAPTURE_INIT = r"""
(() => {
  const NativeWebSocket = window.WebSocket;
  const sockets = [];
  const events = [];
  function WrappedWebSocket(...args) {
    const socket = new NativeWebSocket(...args);
    const index = sockets.push(socket) - 1;
    socket.addEventListener("message", (message) => {
      if (typeof message.data === "string") {
        try {
          events.push({ socket: index, event: JSON.parse(message.data) });
        } catch (_error) {
          events.push({ socket: index, event: { type: "invalid-json" } });
        }
        return;
      }
      if (message.data instanceof ArrayBuffer) {
        const bytes = new Uint8Array(message.data);
        const contains = (needle) => {
          outer: for (let offset = 0; offset + needle.length <= bytes.length; offset += 1) {
            for (let index = 0; index < needle.length; index += 1) {
              if (bytes[offset + index] !== needle[index]) continue outer;
            }
            return true;
          }
          return false;
        };
        const esc = 0x1b;
        const sequence = (text) => new Uint8Array([esc, ...new TextEncoder().encode(text)]);
        events.push({
          socket: index,
          event: {
            type: "binary-diagnostic",
            bytes: bytes.length,
            mouse1000h: contains(sequence("[?1000h")),
            mouse1000l: contains(sequence("[?1000l")),
            mouse1006h: contains(sequence("[?1006h")),
            mouse1006l: contains(sequence("[?1006l")),
            bracket2004h: contains(sequence("[?2004h")),
            bracket2004l: contains(sequence("[?2004l")),
            alt1049h: contains(sequence("[?1049h")),
            alt1049l: contains(sequence("[?1049l")),
          },
        });
      }
    });
    return socket;
  }
  WrappedWebSocket.prototype = NativeWebSocket.prototype;
  Object.setPrototypeOf(WrappedWebSocket, NativeWebSocket);
  for (const name of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
    Object.defineProperty(WrappedWebSocket, name, { value: NativeWebSocket[name] });
  }
  Object.defineProperty(window, "__termdeckTestSockets", { value: sockets });
  Object.defineProperty(window, "__termdeckTestEvents", { value: events });
  window.WebSocket = WrappedWebSocket;
})();
"""
BUFFER_SCRIPT = r"""
() => {
  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
  if (!terminal) return null;
  function read(buffer) {
    const lines = [];
    for (let y = 0; y < buffer.length; y += 1) {
      const line = buffer.getLine(y);
      if (line) lines.push(line.translateToString(true));
    }
    return lines.join('\n');
  }
  return {
    activeType: terminal.buffer.active.type,
    active: read(terminal.buffer.active),
    normal: read(terminal.buffer.normal),
    alternate: read(terminal.buffer.alternate),
    cols: terminal.cols,
    rows: terminal.rows,
  };
}
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def chromium_root_processes() -> dict[int, dict[str, Any]]:
    """Read-only identity snapshot of Playwright Chromium root processes."""
    processes: dict[int, dict[str, Any]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if b"--remote-debugging-pipe" not in raw or b"--type=" in raw:
                continue
            fields = (entry / "stat").read_text(encoding="utf-8").split()
            pid = int(entry.name)
            processes[pid] = {
                "pid": pid,
                "start_ticks": fields[21],
                "cmdline_sha256": hashlib.sha256(raw).hexdigest(),
            }
        except (OSError, ValueError, IndexError):
            continue
    return processes


def run(
    argv: list[str],
    *,
    timeout: float = 20.0,
    check: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        text=text,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def wait_until(predicate: Callable[[], bool], timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:  # noqa: BLE001 - bounded polling diagnostics
            last_error = error
        time.sleep(0.05)
    suffix = f": {last_error}" if last_error else ""
    raise RuntimeError(f"timed out waiting for {detail}{suffix}")


def wait_http(url: str, timeout: float = 30.0) -> None:
    def ready() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - readiness retries are intentionally broad
            return False

    wait_until(ready, timeout, f"HTTP readiness at {url}")


def write_database(path: Path, tmux_name: str, stress_tmux_name: str, cwd: str) -> None:
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
            INSERT INTO remembered_windows VALUES ('win-e2e', 'Random');
            """
        )
        connection.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "td-fixture", "Codex fixture", cwd, tmux_name, "running",
                    "win-e2e", "Random", 0, 1, 1, "2026-08-28T00:00:00Z", None,
                ),
                (
                    "td-stress", "Large history", cwd, stress_tmux_name, "running",
                    "win-e2e", "Random", 1, 1, 0, "2026-08-28T00:00:00Z", None,
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def write_fixture(path: Path) -> None:
    path.write_text(
        r'''#!/usr/bin/python3
from __future__ import annotations
import fcntl, json, os, signal, struct, sys, termios, time, tty
from pathlib import Path

log_path=Path(sys.argv[1])
mode=sys.argv[2] if len(sys.argv)>2 else "interactive"
log_path.parent.mkdir(parents=True,exist_ok=True)
sequence=0

def record(kind, **values):
    payload={"time_ns":time.time_ns(),"kind":kind,**values}
    with log_path.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps(payload,sort_keys=True)+"\n")
        handle.flush(); os.fsync(handle.fileno())

def geometry():
    try:
        rows,cols,_,_=struct.unpack("HHHH",fcntl.ioctl(0,termios.TIOCGWINSZ,b"\0"*8))
        return int(cols),int(rows)
    except OSError:
        return 0,0

def on_winch(_signum,_frame):
    cols,rows=geometry(); record("size",cols=cols,rows=rows,reason="SIGWINCH")

signal.signal(signal.SIGWINCH,on_winch)
old=termios.tcgetattr(0)
tty.setraw(0,when=termios.TCSANOW)
try:
    cols,rows=geometry(); record("size",cols=cols,rows=rows,reason="startup")
    if mode=="stress":
        os.write(1,b"\x1b[2J\x1b[HSTRESS_LONG_START:")
        remaining=700_000
        block=b"0123456789abcdef"*4096
        while remaining>0:
            chunk=block[:min(remaining,len(block))]
            os.write(1,chunk); remaining-=len(chunk)
        os.write(1,b"\r\nSTRESS_READY\r\n")
        record("stress-output",bytes=700_000)
    else:
        os.write(1,b"\x1b[2J\x1b[HPRIMARY_BUFFER_OK\r\n")
        os.write(1,b"\x1b[?1049h\x1b[2J\x1b[H")
        os.write(1,"ALT_SCREEN_OK\r\nUNICODE=東京🙂 COMBINING=é\r\n".encode())
        os.write(1,b"CODEX_COMPOSER> \x1b[38;5;45mfn main() { finish_task(); }\x1b[0m\r\n")
        os.write(1,b"\x1b[?2004h\x1b[?1000h\x1b[?1006h")
    while True:
        data=os.read(0,65536)
        if not data:
            time.sleep(0.01); continue
        record("input",hex=data.hex(),length=len(data))
        if b"\x1c" in data:
            os.write(1,b"\x1b[?1049l\r\nPRIMARY_RESTORED_OK\r\n\x1b[?2004h\x1b[?1000h\x1b[?1006h")
        if b"\x1d" in data:
            sequence+=1
            marker=f"RECONNECT_MARKER_{sequence}"
            os.write(1,("\r\n\x1b[38;5;82m"+marker+"\x1b[0m\r\n").encode())
except BaseException as error:
    record("exit",error=repr(error))
    raise
finally:
    termios.tcsetattr(0,termios.TCSANOW,old)
''',
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_tmux_wrapper(path: Path, socket_name: str) -> None:
    path.write_text(
        f'#!/bin/sh\nexec /usr/bin/tmux -L {socket_name!s} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_t1000_wrapper(
    path: Path,
    socket_name: str,
    fixture: Path,
    logs_dir: Path,
    counter: Path,
) -> None:
    config = {
        "socket": socket_name,
        "fixture": str(fixture),
        "logs": str(logs_dir),
        "counter": str(counter),
    }
    config_path = path.with_suffix(".json")
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    path.write_text(
        r'''#!/usr/bin/python3
from __future__ import annotations
import json, sqlite3, subprocess, sys, time
from pathlib import Path

config=json.loads(Path(__file__).with_suffix(".json").read_text())
args=sys.argv[1:]
if not args: raise SystemExit("missing command")
command=args.pop(0)

def take_option(name, default=None):
    if name not in args: return default
    index=args.index(name)
    if index+1>=len(args): raise SystemExit(f"missing value for {name}")
    value=args[index+1]; del args[index:index+2]; return value

def take_flag(name):
    if name not in args: return False
    args.remove(name); return True

def tmux(*values, check=True):
    return subprocess.run(["/usr/bin/tmux","-L",config["socket"],*values],text=True,capture_output=True,check=check,timeout=20)

db=take_option("--db")
if not db: raise SystemExit("missing --db")
connection=sqlite3.connect(db,timeout=10)
connection.row_factory=sqlite3.Row
try:
    if command=="new":
        cwd=take_option("--cwd")
        window_id=take_option("--window-id")
        window_title=take_option("--window-title")
        name=take_option("--name","Browser tab")
        _requested=take_option("--command",None)
        pinned=1 if take_flag("--pinned") else 0
        counter=Path(config["counter"])
        value=int(counter.read_text())+1 if counter.exists() else 1
        counter.write_text(str(value))
        session_id=f"td-e2e-{value}"
        tmux_name=f"termdeck_e2e_{value}"
        log=Path(config["logs"])/f"{session_id}.jsonl"
        tmux("-f","/dev/null","new-session","-d","-s",tmux_name,"-x","100","-y","30","/usr/bin/python3",config["fixture"],str(log),"interactive")
        index=connection.execute("select coalesce(max(tab_index),-1)+1 from sessions where window_id=?",(window_id,)).fetchone()[0]
        connection.execute(
          "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?)",
          (session_id,name,cwd,tmux_name,"running",window_id,window_title,index,1,pinned,"2026-08-28T00:00:00Z",None),
        )
        connection.commit()
        payload={"tabs":[{"id":session_id,"name":name,"cwd":cwd,"pinned":bool(pinned)}]}
    elif command=="rename":
        session_id,name=args[-2],args[-1]
        connection.execute("update sessions set name=? where id=?",(name,session_id)); connection.commit()
        payload={"tabs":[{"id":session_id,"name":name}]}
    elif command in {"pause","resume"}:
        take_flag("--yes")
        session_id=args[-1]
        status="paused" if command=="pause" else "running"
        connection.execute("update sessions set status=? where id=?",(status,session_id)); connection.commit()
        payload={"tabs":[{"id":session_id,"status":status}]}
    elif command in {"pin","unpin"}:
        session_id=args[-1]; pinned=1 if command=="pin" else 0
        connection.execute("update sessions set pinned=? where id=?",(pinned,session_id)); connection.commit()
        payload={"tabs":[{"id":session_id,"pinned":bool(pinned)}]}
    elif command=="hide":
        take_flag("--include-pinned")
        session_id=args[-1]
        connection.execute("update sessions set visible=0 where id=?",(session_id,)); connection.commit()
        payload={"tabs":[{"id":session_id,"hidden":True}]}
    elif command=="kill":
        take_flag("--yes"); take_flag("--include-pinned")
        session_id=args[-1]
        row=connection.execute("select tmux_name from sessions where id=?",(session_id,)).fetchone()
        if row: tmux("kill-session","-t",row["tmux_name"],check=False)
        connection.execute("update sessions set status='stopped',visible=0 where id=?",(session_id,)); connection.commit()
        payload={"tabs":[{"id":session_id,"killed":True}]}
    else:
        raise SystemExit(f"unsupported fixture command: {command}")
    print(json.dumps(payload,sort_keys=True))
finally:
    connection.close()
''',
        encoding="utf-8",
    )
    path.chmod(0o700)


def create_invitation(binary: Path, auth_dir: Path, label: str) -> str:
    output = run(
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
        timeout=30,
    ).stdout
    value = json.loads(output)
    return str(value["invitation_secret"])


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
            raise RuntimeError(f"pairing failed: HTTP {response.status}")
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
    if {item["name"] for item in cookies} != {"termdeck_session", "termdeck_csrf"}:
        raise RuntimeError("pairing did not issue the expected session and CSRF cookies")
    return cookies


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def input_bytes(path: Path) -> bytes:
    output = bytearray()
    for event in read_log(path):
        if event.get("kind") == "input":
            output.extend(bytes.fromhex(str(event.get("hex", ""))))
    return bytes(output)


def wait_input(path: Path, expected: bytes, timeout: float = 10.0) -> None:
    wait_until(lambda: expected in input_bytes(path), timeout, f"fixture bytes {expected!r}")


def wait_any_input(path: Path, alternatives: tuple[bytes, ...], timeout: float = 10.0) -> None:
    wait_until(
        lambda: any(value in input_bytes(path) for value in alternatives),
        timeout,
        f"fixture bytes among {alternatives!r}",
    )


def terminal_buffer(page: Page) -> dict[str, Any]:
    value = page.evaluate(BUFFER_SCRIPT)
    if not isinstance(value, dict):
        raise RuntimeError("xterm buffer API is unavailable")
    return value


def wait_buffer(page: Page, marker: str, timeout: float = 15.0) -> None:
    wait_until(
        lambda: marker in str((page.evaluate(BUFFER_SCRIPT) or {}).get("active", "")),
        timeout,
        f"xterm marker {marker}",
    )


def wait_live(page: Page, timeout: float = 15.0) -> None:
    page.wait_for_function(
        "() => document.querySelector('#terminal-status')?.textContent === 'Live'",
        timeout=timeout * 1000,
    )


def latest_control(page: Page) -> dict[str, Any] | None:
    value = page.evaluate(
        r"""() => {
          const events = window.__termdeckTestEvents || [];
          for (let index = events.length - 1; index >= 0; index -= 1) {
            const event = events[index].event;
            if (event?.control?.schema === 'termdeck.terminal-control.v1') return event.control;
          }
          return null;
        }"""
    )
    return value if isinstance(value, dict) else None


def wait_mine(page: Page, mine: bool, timeout: float = 10.0) -> None:
    wait_until(lambda: (latest_control(page) or {}).get("mine") is mine, timeout, f"mine={mine}")


def send_manual_frame(page: Page, sequence: int, text: str) -> None:
    page.evaluate(
        r"""({sequence, text}) => {
          const sockets = window.__termdeckTestSockets || [];
          const socket = [...sockets].reverse().find(candidate => candidate.readyState === WebSocket.OPEN);
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


def open_context(playwright: Playwright, browser: Any, cookies: list[dict[str, Any]]) -> BrowserContext:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.add_init_script(SOCKET_CAPTURE_INIT)
    context.add_cookies(cookies)
    return context


def sqlite_row(database: Path, session_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("select * from sessions where id=?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def click_action(page: Page, label: str) -> None:
    page.locator("details.workspace-actions summary").click()
    page.get_by_role("button", name=label, exact=True).click()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--binary",
        type=Path,
        default=ROOT / ".ai-bridge/target/debug/termdeck-web-server",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / ".ai-bridge/final-evidence/browser-e2e",
    )
    parser.add_argument("--max-live-ms", type=float, default=5000.0)
    args = parser.parse_args()
    binary = args.binary.resolve()
    evidence = args.evidence.resolve()
    if not binary.is_file():
        raise SystemExit(f"server binary is missing: {binary}")
    if evidence.exists():
        shutil.rmtree(evidence)
    evidence.mkdir(parents=True, mode=0o700)

    run_parent = ROOT / ".ai-bridge/tmp"
    run_parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="browser-e2e-", dir=run_parent))
    browser_home = run_root / "home"
    browser_home.mkdir(mode=0o700)
    (browser_home / ".config").mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(browser_home),
            "XDG_CONFIG_HOME": str(browser_home / ".config"),
            "TMPDIR": "/tmp",
        }
    )
    os.environ.update({key: environment[key] for key in ("HOME", "XDG_CONFIG_HOME", "TMPDIR")})
    tempfile.tempdir = None

    socket_name = f"termdeck-browser-e2e-{os.getpid()}"
    initial_tmux = "termdeck_fixture_random"
    stress_tmux = "termdeck_fixture_stress"
    initial_log = run_root / "logs/td-fixture.jsonl"
    stress_log = run_root / "logs/td-stress.jsonl"
    fixture = run_root / "fixture.py"
    tmux_wrapper = run_root / "tmux"
    t1000_wrapper = run_root / "t1000"
    database = run_root / "sessions.sqlite3"
    auth_parent = run_root / "auth-parent"
    auth_parent.mkdir(mode=0o700)
    auth_dir = auth_parent / "auth"
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    server_stdout_path = evidence / "server.stdout"
    server_stderr_path = evidence / "server.stderr"
    server_stdout = server_stdout_path.open("wb")
    server_stderr = server_stderr_path.open("wb")
    server: subprocess.Popen[bytes] | None = None
    browsers: list[Any] = []
    contexts: list[BrowserContext] = []
    page_one: Page | None = None
    page_two: Page | None = None
    browser_processes: list[dict[str, Any]] = []
    console: list[dict[str, str]] = []
    page_errors: list[str] = []
    request_failures: list[str] = []
    timings: dict[str, float] = {}
    checks: dict[str, Any] = {}
    started_ns = time.monotonic_ns()

    try:
        write_fixture(fixture)
        write_tmux_wrapper(tmux_wrapper, socket_name)
        write_t1000_wrapper(
            t1000_wrapper,
            socket_name,
            fixture,
            run_root / "logs",
            run_root / "counter",
        )
        run(
            [
                "/usr/bin/tmux",
                "-L",
                socket_name,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                initial_tmux,
                "-x",
                "100",
                "-y",
                "30",
                "/usr/bin/python3",
                str(fixture),
                str(initial_log),
                "interactive",
            ],
            env=environment,
        )
        run(
            [
                "/usr/bin/tmux", "-L", socket_name, "-f", "/dev/null",
                "new-session", "-d", "-s", stress_tmux, "-x", "100", "-y", "30",
                "/usr/bin/python3", str(fixture), str(stress_log), "stress",
            ],
            env=environment,
        )
        wait_until(lambda: initial_log.exists(), 10, "initial fixture log")
        wait_until(
            lambda: any(event.get("kind") == "stress-output" for event in read_log(stress_log)),
            20,
            "pathological history fixture",
        )
        write_database(database, initial_tmux, stress_tmux, str(ROOT))
        invitation_one = create_invitation(binary, auth_dir, "Disposable browser one")
        invitation_two = create_invitation(binary, auth_dir, "Disposable browser two")
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
            "64",
            "--tmux",
            str(tmux_wrapper),
            "--tmux-timeout-ms",
            "2000",
            "--t1000",
            str(t1000_wrapper),
            "--tab-action-timeout-ms",
            "10000",
            "--host-id",
            "host-browser-e2e",
            "--owner-id",
            "owner-browser-e2e",
            "--auth-dir",
            str(auth_dir),
            "--allow-insecure-loopback-auth",
        ]
        server = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=server_stdout,
            stderr=server_stderr,
            start_new_session=True,
        )
        wait_http(f"{origin}/_termdeck/healthz")
        timings["server_ready_ms"] = (time.monotonic_ns() - started_ns) / 1_000_000
        cookies_one = pair(origin, invitation_one)
        cookies_two = pair(origin, invitation_two)

        with sync_playwright() as playwright:
            chromium_before = chromium_root_processes()
            launch_options = {
                "executable_path": "/usr/bin/chromium",
                "headless": True,
                "env": environment,
                "args": [
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                ],
            }
            # Measure the primary terminal with one browser process. Launching
            # the takeover browser before first paint made the latency metric a
            # self-inflicted two-Chromium contention test on busy hosts.
            browser_one = playwright.chromium.launch(**launch_options)
            browsers.append(browser_one)
            context_one = open_context(playwright, browser_one, cookies_one)
            contexts.append(context_one)
            context_one.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
            page_one = context_one.new_page()
            page_one.on(
                "console",
                lambda message: console.append(
                    {"type": message.type, "text": message.text[:2000]}
                ),
            )
            page_one.on("pageerror", lambda error: page_errors.append(str(error)[:4000]))
            page_one.on(
                "requestfailed",
                lambda request: request_failures.append(
                    f"{request.method} {request.url} {request.failure}"
                ),
            )

            navigation_ns = time.monotonic_ns()
            page_one.goto(f"{origin}/random", wait_until="domcontentloaded", timeout=15000)
            page_one.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=15000)
            timings["xterm_visible_ms"] = (time.monotonic_ns() - navigation_ns) / 1_000_000
            wait_live(page_one)
            timings["terminal_live_ms"] = (time.monotonic_ns() - navigation_ns) / 1_000_000
            if timings["terminal_live_ms"] >= args.max_live_ms:
                raise RuntimeError(
                    f"terminal live latency {timings['terminal_live_ms']:.1f} ms exceeded {args.max_live_ms:.1f} ms"
                )
            wait_buffer(page_one, "ALT_SCREEN_OK")
            first_buffer = terminal_buffer(page_one)
            checks["alternate_screen_active"] = first_buffer["activeType"] == "alternate"
            checks["alternate_marker"] = "ALT_SCREEN_OK" in first_buffer["active"]
            checks["unicode"] = "東京🙂" in first_buffer["active"]
            checks["combining"] = "é" in first_buffer["active"]
            checks["composer_ansi"] = "fn main() { finish_task(); }" in first_buffer["active"]
            if not all(checks.values()):
                raise RuntimeError(f"initial terminal rendering contract failed: {checks}")
            run(
                [
                    "/usr/bin/tmux", "-L", socket_name, "send-keys", "-t",
                    f"{initial_tmux}:0.0", "-H", "1c",
                ],
                env=environment,
            )
            wait_until(
                lambda: (page_one.evaluate(BUFFER_SCRIPT) or {}).get("activeType") == "normal",
                10,
                "alternate-screen exit",
            )
            wait_buffer(page_one, "PRIMARY_RESTORED_OK", timeout=10)
            restored_buffer = terminal_buffer(page_one)
            checks["normal_buffer_marker"] = "PRIMARY_RESTORED_OK" in restored_buffer["active"]
            if not checks["normal_buffer_marker"]:
                raise RuntimeError("primary screen was not restored after alternate-screen exit")

            stress_started = time.monotonic_ns()
            page_one.locator('[data-pane-id="td-stress"] .workspace-tab-select').click()
            page_one.wait_for_url(re.compile(r"/random/t/td-stress$"), timeout=10000)
            wait_live(page_one, timeout=20)
            wait_buffer(page_one, "STRESS_READY", timeout=20)
            timings["large_history_live_ms"] = (time.monotonic_ns() - stress_started) / 1_000_000
            if timings["large_history_live_ms"] >= 12000:
                raise RuntimeError(
                    f"large-history terminal latency {timings['large_history_live_ms']:.1f} ms exceeded 12000 ms"
                )
            checks["large_unbroken_history"] = True
            page_one.locator('[data-pane-id="td-fixture"] .workspace-tab-select').click()
            page_one.wait_for_url(re.compile(r"/random/t/td-fixture$"), timeout=10000)
            wait_live(page_one, timeout=20)
            wait_buffer(page_one, "PRIMARY_RESTORED_OK", timeout=20)

            page_html = page_one.content()
            for rejected in REJECTED_UI:
                if rejected in page_html:
                    raise RuntimeError(f"rejected UI string is visible: {rejected}")
            checks["new_tab_button"] = page_one.get_by_role("button", name="New tab", exact=True).count() == 1
            checks["stable_window_route"] = page_one.url in {
                f"{origin}/random",
                f"{origin}/random/t/td-fixture",
            }
            page_one.locator("#terminal-xterm").click(position={"x": 30, "y": 30})
            wait_mine(page_one, True)

            textarea = page_one.locator("#terminal-xterm .xterm-helper-textarea")
            textarea.focus()
            textarea.type("DIRECT_TEXT_123", delay=1)
            textarea.press("Enter")
            textarea.press("Tab")
            textarea.press("Escape")
            textarea.press("ArrowUp")
            textarea.press("ArrowDown")
            textarea.press("ArrowLeft")
            textarea.press("ArrowRight")
            textarea.press("Control+L")
            textarea.press("Control+C")
            textarea.press("Control+D")
            wait_input(initial_log, b"DIRECT_TEXT_123")
            wait_input(initial_log, b"\r")
            wait_input(initial_log, b"\t")
            wait_any_input(initial_log, (b"\x1b[A", b"\x1bOA"))
            wait_any_input(initial_log, (b"\x1b[B", b"\x1bOB"))
            wait_any_input(initial_log, (b"\x1b[D", b"\x1bOD"))
            wait_any_input(initial_log, (b"\x1b[C", b"\x1bOC"))
            wait_input(initial_log, b"\x0c")
            wait_input(initial_log, b"\x03")
            wait_input(initial_log, b"\x04")
            checks["keyboard_exact_bytes"] = True

            paste_text = "PASTE_日本語_🙂_END"
            page_one.evaluate("text => navigator.clipboard.writeText(text)", paste_text)
            textarea.focus()
            textarea.press("Control+V")
            paste_bytes = b"\x1b[200~" + paste_text.encode("utf-8") + b"\x1b[201~"
            try:
                wait_input(initial_log, paste_bytes, timeout=3)
            except RuntimeError:
                textarea.evaluate(
                    """(element, text) => {
                      const transfer = new DataTransfer();
                      transfer.setData('text/plain', text);
                      element.dispatchEvent(new ClipboardEvent('paste', {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: transfer,
                      }));
                    }""",
                    paste_text,
                )
                wait_input(initial_log, paste_bytes, timeout=7)
            checks["clipboard_bracketed_paste"] = True

            mouse_mode = page_one.evaluate(
                "() => document.querySelector('#terminal-xterm').termdeckTerminal.terminal.modes.mouseTrackingMode"
            )
            pane_mouse_state = run(
                [
                    "/usr/bin/tmux", "-L", socket_name, "display-message", "-p", "-t",
                    initial_tmux,
                    "#{mouse_standard_flag}:#{mouse_button_flag}:#{mouse_all_flag}:#{mouse_sgr_flag}",
                ],
                env=environment,
            ).stdout.strip()
            checks["mouse_mode_restored"] = mouse_mode != "none" and pane_mouse_state.endswith(":1")
            if not checks["mouse_mode_restored"]:
                raise RuntimeError(
                    f"xterm mouse tracking was not restored: mode={mouse_mode} tmux={pane_mouse_state}"
                )
            screen = page_one.locator("#terminal-xterm .xterm-screen")
            box = screen.bounding_box()
            if not box:
                raise RuntimeError("xterm screen has no bounding box")
            mouse_start = len(input_bytes(initial_log))
            page_one.mouse.move(box["x"] + box["width"] * 0.40, box["y"] + box["height"] * 0.40)
            page_one.mouse.down(button="left")
            page_one.mouse.move(
                box["x"] + box["width"] * 0.55,
                box["y"] + box["height"] * 0.55,
                steps=3,
            )
            page_one.mouse.up(button="left")
            page_one.mouse.wheel(0, 160)
            wait_until(
                lambda: bool(
                    re.search(
                        br"\x1b\[<(?:64|65);\d+;\d+M",
                        input_bytes(initial_log)[mouse_start:],
                    )
                ),
                10,
                f"SGR mouse wheel report (mode={mouse_mode}, tmux={pane_mouse_state})",
            )
            mouse_bytes = input_bytes(initial_log)[mouse_start:]
            checks["mouse_sgr_click"] = bool(re.search(br"\x1b\[<0;\d+;\d+[Mm]", mouse_bytes))
            checks["mouse_wheel"] = bool(
                re.search(br"\x1b\[<(?:64|65);\d+;\d+M", mouse_bytes)
            )
            if not checks["mouse_sgr_click"] or not checks["mouse_wheel"]:
                raise RuntimeError(
                    f"incomplete SGR mouse proof: mode={mouse_mode} tmux={pane_mouse_state} bytes={mouse_bytes[-1000:]!r}"
                )

            initial_sizes = [
                (event.get("cols"), event.get("rows"))
                for event in read_log(initial_log)
                if event.get("kind") == "size"
            ]
            canonical_size = run(
                [
                    "/usr/bin/tmux", "-L", socket_name, "display-message", "-p", "-t",
                    initial_tmux, "#{pane_width}x#{pane_height}",
                ],
                env=environment,
            ).stdout.strip()
            page_one.set_viewport_size({"width": 1024, "height": 640})
            page_one.wait_for_timeout(500)
            compact_size = run(
                [
                    "/usr/bin/tmux", "-L", socket_name, "display-message", "-p", "-t",
                    initial_tmux, "#{pane_width}x#{pane_height}",
                ],
                env=environment,
            ).stdout.strip()
            page_one.set_viewport_size({"width": 1680, "height": 1050})
            page_one.wait_for_timeout(500)
            large_size = run(
                [
                    "/usr/bin/tmux", "-L", socket_name, "display-message", "-p", "-t",
                    initial_tmux, "#{pane_width}x#{pane_height}",
                ],
                env=environment,
            ).stdout.strip()
            final_sizes = [
                (event.get("cols"), event.get("rows"))
                for event in read_log(initial_log)
                if event.get("kind") == "size"
            ]
            browser_grid = page_one.evaluate(
                """() => {
                  const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
                  return terminal ? `${terminal.cols}x${terminal.rows}` : null;
                }"""
            )
            checks["browser_geometry_isolated"] = (
                compact_size == canonical_size
                and large_size == canonical_size
                and browser_grid == canonical_size
                and set(final_sizes) == set(initial_sizes)
            )
            if not checks["browser_geometry_isolated"]:
                raise RuntimeError(
                    f"browser viewport changed shared terminal geometry: canonical={canonical_size}, "
                    f"compact={compact_size}, large={large_size}, browser={browser_grid}, "
                    f"initial_events={initial_sizes}, final_events={final_sizes}"
                )

            first_control = latest_control(page_one) or {}
            first_sequence = int(first_control.get("next_sequence", 1))
            browser_two = playwright.chromium.launch(**launch_options)
            browsers.append(browser_two)
            wait_until(
                lambda: len(set(chromium_root_processes()) - set(chromium_before)) >= 2,
                10,
                "two independent Chromium root processes",
            )
            chromium_after = chromium_root_processes()
            browser_processes = [
                chromium_after[pid]
                for pid in sorted(set(chromium_after) - set(chromium_before))
            ]
            checks["separate_browser_processes"] = len(browser_processes) >= 2
            if not checks["separate_browser_processes"]:
                raise RuntimeError(f"two-browser process proof failed: {browser_processes}")
            context_two = open_context(playwright, browser_two, cookies_two)
            contexts.append(context_two)
            page_two = context_two.new_page()
            page_two.on(
                "console",
                lambda message: console.append(
                    {"type": message.type, "text": message.text[:2000]}
                ),
            )
            page_two.on("pageerror", lambda error: page_errors.append(str(error)[:4000]))
            page_two.on(
                "requestfailed",
                lambda request: request_failures.append(
                    f"{request.method} {request.url} {request.failure}"
                ),
            )
            page_two.goto(f"{origin}/random/t/td-fixture", wait_until="domcontentloaded", timeout=15000)
            page_two.locator("#terminal-xterm .xterm").wait_for(state="visible", timeout=15000)
            wait_live(page_two)
            try:
                wait_buffer(page_two, "PRIMARY_RESTORED_OK")
            except RuntimeError:
                diagnostic = {
                    "page_two_terminal": page_two.evaluate(BUFFER_SCRIPT),
                    "page_two_socket_events": page_two.evaluate("() => window.__termdeckTestEvents || []"),
                    "page_two_status": page_two.locator("#terminal-status").get_attribute("aria-label"),
                    "tmux_capture": run(
                        ["/usr/bin/tmux", "-L", socket_name, "capture-pane", "-epq", "-t", initial_tmux, "-N"],
                        env=environment,
                    ).stdout,
                    "tmux_state": run(
                        ["/usr/bin/tmux", "-L", socket_name, "display-message", "-p", "-t", initial_tmux,
                         "#{alternate_on}:#{cursor_x}:#{cursor_y}:#{history_size}:#{pane_width}:#{pane_height}"],
                        env=environment,
                    ).stdout.strip(),
                }
                (evidence / "page-two-replay-diagnostic.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                raise
            page_two.locator("#terminal-xterm .xterm-helper-textarea").focus()
            page_two.locator("#terminal-xterm").click(position={"x": 20, "y": 20})
            wait_mine(page_two, True)
            checks["explicit_focus_takeover"] = True
            before_stale = input_bytes(initial_log)
            stale_marker = "STALE_WRITER_MUST_NOT_ARRIVE"
            send_manual_frame(page_one, first_sequence, stale_marker)
            time.sleep(0.8)
            after_stale = input_bytes(initial_log)
            checks["stale_writer_rejected"] = stale_marker.encode() not in after_stale[len(before_stale):]
            if not checks["stale_writer_rejected"]:
                raise RuntimeError("stale writer bytes reached the terminal")

            page_one.locator("#terminal-xterm").click(position={"x": 30, "y": 30})
            wait_mine(page_one, True)
            page_one.locator("#terminal-xterm .xterm-helper-textarea").type("TAKEBACK_OK")
            wait_input(initial_log, b"TAKEBACK_OK")
            checks["explicit_focus_retake"] = True

            socket_count = int(page_one.evaluate("() => window.__termdeckTestSockets.length"))
            page_one.evaluate(
                r"""() => {
                  const socket=[...window.__termdeckTestSockets].reverse().find(item => item.readyState===WebSocket.OPEN);
                  if (!socket) throw new Error('no socket to close');
                  socket.close(4000, 'disposable reconnect proof');
                }"""
            )
            page_one.wait_for_function(
                """() => {
                  const value=document.querySelector('#terminal-status')?.textContent || '';
                  return value.startsWith('Reconnecting') || value === 'Connecting…';
                }""",
                timeout=5000,
            )
            run(
                [
                    "/usr/bin/tmux",
                    "-L",
                    socket_name,
                    "send-keys",
                    "-t",
                    f"{initial_tmux}:0.0",
                    "-H",
                    "1d",
                ],
                env=environment,
            )
            wait_until(
                lambda: any(
                    event.get("kind") == "input" and "1d" in str(event.get("hex", ""))
                    for event in read_log(initial_log)
                ),
                5,
                "fixture reconnect marker command",
            )
            wait_live(page_one, timeout=20)
            wait_until(
                lambda: int(page_one.evaluate("() => window.__termdeckTestSockets.length")) > socket_count,
                20,
                "new WebSocket after reconnect",
            )
            wait_buffer(page_one, "RECONNECT_MARKER_1", timeout=20)
            checks["reconnect_resynchronization"] = True

            page_one.evaluate("""() => {
              const button = document.querySelector('.workspace-new-tab');
              button.click(); button.click();
            }""")
            if page_one.locator('dialog[open]').count() or page_one.locator('input[name="cwd"]').count():
                raise RuntimeError("New tab unexpectedly opened a configuration form")
            page_one.wait_for_url(re.compile(r"/random/t/td-e2e-1$"), timeout=15000)
            page_one.locator('.workspace-tab[data-pane-id="td-e2e-1"]').wait_for(state="visible", timeout=15000)
            wait_live(page_one, timeout=20)
            wait_buffer(page_one, "ALT_SCREEN_OK", timeout=20)
            row = sqlite_row(database, "td-e2e-1")
            if not row or row["cwd"] != str(browser_home) or row["pinned"] != 0:
                raise RuntimeError(f"new tab registry state is wrong: {row}")
            if (run_root / "counter").read_text() != "1":
                raise RuntimeError("repeated New tab click created duplicate tabs")
            checks["new_tab"] = True
            checks["stable_tab_route"] = page_one.url.endswith("/random/t/td-e2e-1")
            create_url = f"{origin}/_termdeck/api/v1/windows/random/tabs"
            request_headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin",
                               "Sec-Fetch-Mode": "cors", "Content-Type": "application/json"}
            rejected = page_one.context.request.post(create_url, data="{}", headers=request_headers)
            if rejected.status != 403:
                raise RuntimeError(f"new-tab mutation without CSRF returned {rejected.status}")
            csrf = next(cookie["value"] for cookie in page_one.context.cookies() if cookie["name"] == "termdeck_csrf")
            rejected = page_one.context.request.post(
                create_url, data='{"cwd":"relative"}',
                headers={**request_headers, "X-TermDeck-CSRF": csrf},
            )
            if rejected.status != 400:
                raise RuntimeError(f"relative server cwd returned {rejected.status}")
            checks["new_tab_csrf_and_cwd_denials"] = True

            page_one.locator('details.workspace-actions summary').click()
            page_one.locator('.workspace-action-panel').get_by_role('button', name='Tab manager').click()
            manager_dialog = page_one.locator('#management-dialog')
            manager_dialog.wait_for(state='visible')
            manager_dialog.locator('#management-title').click()
            if not manager_dialog.is_visible():
                raise RuntimeError('clicking inside the manager dismissed it')
            page_one.mouse.click(5, 5)
            manager_dialog.wait_for(state='hidden')
            page_one.wait_for_timeout(300)
            if manager_dialog.evaluate('dialog => dialog.open'):
                raise RuntimeError(f"manager reopened after backdrop dismissal: {manager_dialog.locator('#management-title').inner_text()}")
            checks['manager_backdrop_dismissal'] = True
            page_one.locator('details.workspace-actions summary').click()
            page_one.locator('.workspace-action-panel').get_by_role('button', name='Client files').click()
            manager_dialog.wait_for(state='visible')
            if manager_dialog.locator('#management-title').inner_text() != 'Client files':
                raise RuntimeError('Client files popup did not open')
            page_one.mouse.click(5, 5)
            manager_dialog.wait_for(state='hidden')
            checks['client_files_backdrop_dismissal'] = True

            click_action(page_one, "Rename tab")
            action_dialog = page_one.locator("#workspace-action-dialog")
            action_dialog.wait_for(state="visible")
            action_dialog.locator('input[name="rename"]').fill("Renamed browser tab")
            action_dialog.get_by_role("button", name="Save name").click()
            page_one.get_by_role("tab", name=re.compile("Renamed browser tab")).wait_for(timeout=15000)
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("name") == "Renamed browser tab",
                10,
                "renamed tab in registry",
            )
            checks["rename_tab"] = True

            click_action(page_one, "Pause")
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("status") == "paused",
                10,
                "paused tab in registry",
            )
            checks["pause_tab"] = True
            click_action(page_one, "Resume")
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("status") == "running",
                10,
                "resumed tab in registry",
            )
            checks["resume_tab"] = True
            click_action(page_one, "Pin")
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("pinned") == 1,
                10,
                "pinned tab in registry",
            )
            checks["pin_tab"] = True
            click_action(page_one, "Unpin")
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("pinned") == 0,
                10,
                "unpinned tab in registry",
            )
            checks["unpin_tab"] = True

            hidden_row = sqlite_row(database, "td-e2e-1")
            if not hidden_row:
                raise RuntimeError("browser lifecycle tab vanished before hide")
            hidden_tmux = hidden_row["tmux_name"]
            click_action(page_one, "Hide tab")
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-1") or {}).get("visible") == 0,
                10,
                "hidden tab registry state",
            )
            page_one.locator('.workspace-tab[data-pane-id="td-e2e-1"]').wait_for(state="detached", timeout=15000)
            hidden_alive = subprocess.run(
                ["/usr/bin/tmux", "-L", socket_name, "has-session", "-t", hidden_tmux],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode == 0
            if not hidden_alive:
                raise RuntimeError("browser close killed the hidden terminal process")
            checks["hide_tab_preserves_process"] = True
            checks["replacement_tab_selected"] = page_one.locator('.workspace-tab[data-pane-id="td-fixture"]').count() == 1

            page_one.get_by_role("button", name="New tab", exact=True).click()
            page_one.wait_for_url(re.compile(r"/random/t/td-e2e-2$"), timeout=15000)
            page_one.locator('.workspace-tab[data-pane-id="td-e2e-2"]').wait_for(state="visible", timeout=15000)
            kill_row = sqlite_row(database, "td-e2e-2")
            if not kill_row:
                raise RuntimeError("kill target was not created")
            kill_tmux = kill_row["tmux_name"]
            click_action(page_one, "Kill process…")
            action_dialog.wait_for(state="visible")
            action_dialog.get_by_role("button", name="Cancel").click()
            action_dialog.wait_for(state="hidden")
            if (sqlite_row(database, "td-e2e-2") or {}).get("status") != "running":
                raise RuntimeError("canceling Kill changed the disposable process")
            click_action(page_one, "Kill process…")
            action_dialog.wait_for(state="visible")
            page_one.mouse.click(5, 5)
            action_dialog.wait_for(state="hidden")
            if (sqlite_row(database, "td-e2e-2") or {}).get("status") != "running":
                raise RuntimeError("backdrop dismissal changed the disposable process")
            checks["backdrop_dismisses_without_action"] = True
            click_action(page_one, "Kill process…")
            action_dialog.wait_for(state="visible")
            action_dialog.get_by_role("button", name="Kill process", exact=True).click()
            wait_until(
                lambda: (sqlite_row(database, "td-e2e-2") or {}).get("status") == "stopped"
                and (sqlite_row(database, "td-e2e-2") or {}).get("visible") == 0,
                10,
                "killed tab registry state",
            )
            page_one.locator('.workspace-tab[data-pane-id="td-e2e-2"]').wait_for(state="detached", timeout=15000)
            killed_alive = subprocess.run(
                ["/usr/bin/tmux", "-L", socket_name, "has-session", "-t", kill_tmux],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode == 0
            if killed_alive:
                raise RuntimeError("explicit kill left the disposable process alive")
            checks["kill_process"] = True

            unauthenticated = browser_one.new_context(viewport={"width": 800, "height": 600})
            try:
                unauth_page = unauthenticated.new_page()
                unauth_page.goto(f"{origin}/random", wait_until="domcontentloaded", timeout=10000)
                checks["unauthenticated_redirect"] = any(
                    path in unauth_page.url for path in ("/_termdeck/login", "/_termdeck/setup")
                )
                denied = unauthenticated.request.post(create_url, data="{}", headers=request_headers)
                checks["unauthenticated_new_tab_denied"] = denied.status == 401
            finally:
                unauthenticated.close()
            if not checks["unauthenticated_redirect"]:
                raise RuntimeError("unauthenticated browser was not redirected")

            screenshot = evidence / "terminal-final.png"
            page_one.screenshot(path=str(screenshot), full_page=True)
            checks["screenshot_sha256"] = sha256(screenshot)
            checks["screenshot_bytes"] = screenshot.stat().st_size
            final_buffer = terminal_buffer(page_one)
            checks["final_buffer_contains_fixture"] = "PRIMARY_RESTORED_OK" in final_buffer["active"]
            context_one.close()
            context_two.close()
            contexts.clear()
            for browser_item in browsers:
                browser_item.close()
            browsers.clear()

        if page_errors:
            raise RuntimeError(f"browser page errors: {page_errors}")
        if request_failures:
            raise RuntimeError(f"browser request failures: {request_failures}")
        failed_checks = [name for name, value in checks.items() if value is False]
        if failed_checks:
            raise RuntimeError(f"failed browser checks: {failed_checks}")

        shutil.copy2(initial_log, evidence / "td-fixture.jsonl")
        for log in sorted((run_root / "logs").glob("*.jsonl")):
            if log != initial_log:
                shutil.copy2(log, evidence / log.name)
        connection = sqlite3.connect(database)
        try:
            rows = [dict(zip([item[0] for item in cursor.description], row)) for cursor in [connection.execute("select * from sessions order by tab_index,id")] for row in cursor.fetchall()]
        finally:
            connection.close()
        result = {
            "schema": "termdeck.browser-e2e.v1",
            "ok": True,
            "binary": {"path": str(binary), "bytes": binary.stat().st_size, "sha256": sha256(binary)},
            "timings_ms": {key: round(value, 3) for key, value in timings.items()},
            "checks": checks,
            "console": console,
            "page_errors": page_errors,
            "request_failures": request_failures,
            "browser_processes": browser_processes,
            "final_registry_rows": rows,
            "fixture_input_bytes": len(input_bytes(initial_log)),
        }
        (evidence / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except BaseException as error:
        partial = {
            "schema": "termdeck.browser-e2e.partial.v1",
            "ok": False,
            "error": repr(error),
            "timings_ms": {key: round(value, 3) for key, value in timings.items()},
            "checks": checks,
            "console": console,
            "page_errors": page_errors,
            "request_failures": request_failures,
            "browser_processes": browser_processes,
            "page_one_socket_events": [],
            "page_two_socket_events": [],
            "page_one_terminal": None,
            "page_two_terminal": None,
        }
        for label, page_value in (("page_one", page_one), ("page_two", page_two)):
            if page_value is None:
                continue
            try:
                partial[f"{label}_socket_events"] = page_value.evaluate(
                    "() => window.__termdeckTestEvents || []"
                )
                partial[f"{label}_terminal"] = page_value.evaluate(BUFFER_SCRIPT)
                partial[f"{label}_status"] = page_value.evaluate(
                    "() => ({text: document.querySelector('#terminal-status')?.textContent || '', state: document.querySelector('#terminal-status')?.dataset.state || '', overlay: document.querySelector('#terminal-overlay')?.textContent || '', url: location.href, readyState: document.readyState, bodyConnection: document.body.dataset.termdeckConnection || ''})"
                )
                partial[f"{label}_html_tail"] = page_value.content()[-12000:]
                page_value.screenshot(
                    path=str(evidence / f"{label}-failure.png"), full_page=True
                )
            except Exception as diagnostic_error:  # noqa: BLE001 - preserve original failure
                partial[f"{label}_diagnostic_error"] = repr(diagnostic_error)
        (evidence / "partial-result.json").write_text(
            json.dumps(partial, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        for log in sorted((run_root / "logs").glob("*.jsonl")):
            try:
                shutil.copy2(log, evidence / log.name)
            except OSError:
                pass
        raise
    finally:
        for context in contexts:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - cleanup must continue
                pass
        for browser_item in browsers:
            try:
                browser_item.close()
            except Exception:  # noqa: BLE001 - cleanup must continue
                pass
        if server is not None and server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(server.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        server_stdout.close()
        server_stderr.close()
        run(
            ["/usr/bin/tmux", "-L", socket_name, "kill-server"],
            check=False,
            timeout=10,
            env=environment,
        )
        shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    main()
