#!/usr/bin/env python3
"""Check native Codex conversation scrolling in a disposable authenticated web app.

The fixture has a synthetic saved conversation, private CODEX_HOME and a local
disabled provider. It never reads login credentials or sends a model request.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from playwright.sync_api import sync_playwright

import check_web_terminal_browser as proof

WIRE = r"""(() => {
  const Native = WebSocket;
  window.scrollSockets = [];
  function Socket(...args) {
    const ws = new Native(...args); scrollSockets.push(ws); ws.frames = []; ws.events = [];
    const send = ws.send.bind(ws);
    ws.send = value => { if (typeof value !== 'string') ws.frames.push([...new Uint8Array(value)]); send(value); };
    ws.addEventListener('message', e => {
      if (typeof e.data === 'string') { const v = JSON.parse(e.data); ws.events.push(v); if(v.control) ws.control=v.control; }
    });
    return ws;
  }
  Socket.prototype = Native.prototype; Object.setPrototypeOf(Socket, Native);
  window.WebSocket = Socket;
})();"""


def conversation(home: Path, cwd: Path) -> str:
    identity = str(uuid.uuid4())
    stamp = "2026-09-30T00:00:00Z"
    message = "\n".join(f"CODEX_SCROLL_ROW_{i:04d} disposable conversation text" for i in range(250))
    records = [
        {"type": "session_meta", "payload": {"id": identity, "timestamp": stamp, "cwd": str(cwd),
            "originator": "codex_cli_rs", "cli_version": "0.159.0", "source": "cli", "model_provider": "fixture"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "DISPOSABLE_PROMPT_DO_NOT_RECALL"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": message}]}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "DISPOSABLE_PROMPT_DO_NOT_RECALL",
            "images": [], "local_images": [], "text_elements": []}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": message, "phase": "final_answer"}},
    ]
    directory = home / "sessions/2026/09/30"
    directory.mkdir(mode=0o700, parents=True)
    (directory / f"rollout-2026-09-30T00-00-00-{identity}.jsonl").write_text(
        "".join(json.dumps({"timestamp": stamp, **r}) + "\n" for r in records))
    (home / "config.toml").write_text(
        'model_provider="fixture"\nmodel="fixture"\n[model_providers.fixture]\nname="Fixture"\n'
        'base_url="http://127.0.0.1:1"\nwire_api="responses"\nrequires_openai_auth=false\n'
        '[features]\nremote_models=false\n[projects.' + json.dumps(str(cwd)) + ']\ntrust_level="trusted"\n')
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--codex-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, default=Path("/usr/bin/chromium"))
    args = parser.parse_args()
    binary, codex = args.server_binary.resolve(), args.codex_binary.resolve()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    runtime = proof.ROOT / ".termdeck/qa"; runtime.mkdir(mode=0o700, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="cs-", dir=runtime))
    result = {"checks": {}, "binary_sha256": proof.sha256_file(binary), "codex_sha256": proof.sha256_file(codex)}
    server = tmux = None
    env = os.environ.copy()
    try:
        home = root / "home"; ch = root / "codex"
        for d in (home / ".config", home / ".local/share", home / ".cache", ch, root / "tmux", root / "auth-parent"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = conversation(ch, root)
        logger = root / "logger.py"
        logger.write_text("import os,tty,sys\nfrom pathlib import Path\ntty.setraw(0)\nos.write(1,b'\\x1b[?1049l\\r\\nSTALE_SHELL_READY\\r\\n')\nwhile True:\n b=os.read(0,65536)\n if not b:break\n with Path(sys.argv[1]).open('ab',buffering=0) as f:f.write(b)\n")
        db, auth = root / "sessions.sqlite3", root / "auth-parent/auth"
        tmux, t1000 = proof.write_wrappers(root / "bin", home=home, tmux_tmpdir=root / "tmux", python=Path(sys.executable))
        env.update(HOME=str(home), CODEX_HOME=str(ch), XDG_CONFIG_HOME=str(home / ".config"),
            XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"), TMUX_TMPDIR=str(root / "tmux"),
            PYTHONPATH=str(proof.ROOT), PATH=f"{root / 'bin'}:/usr/local/bin:/usr/bin:/bin", PYTHONNOUSERSITE="1")
        for k in ("TMUX", "TMUX_PANE", "TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY", "OPENAI_API_KEY"):
            env.pop(k, None)
        command = shlex.join([str(codex), "--no-daemon", "resume", identity]) + "; exec " + shlex.join([sys.executable, str(logger), str(root / "stale.raw")])
        tabs = {}
        for label, cmd in [("Codex", command), ("Other", shlex.join([sys.executable, str(logger), str(root / "other.raw")]))]:
            payload = proof.run_json([str(t1000), "new", "--db", str(db), "--cwd", str(root), "--window-id", "win-scroll",
                "--window-title", "Scroll", "--name", label, "--command", cmd], env=env)
            tabs[label] = payload["tabs"][0]["id"]
        result["codex_version"] = subprocess.run([str(codex), "--version"], env=env, text=True, capture_output=True, check=True).stdout.strip()
        origin = f"http://127.0.0.1:{proof.free_port()}"
        secret = proof.invitation(binary, auth, "Scroll fixture")
        with (out / "server.log").open("wb") as log:
            server = subprocess.Popen([str(binary), "--db", str(db), "--listen", origin[7:], "--refresh-ms", "250",
                "--max-stale-ms", "10000", "--max-connections", "128", "--tmux", str(tmux), "--tmux-timeout-ms", "3000",
                "--t1000", str(t1000), "--auth-dir", str(auth), "--host-id", "host-browser-e2e", "--owner-id", "owner-browser-e2e",
                "--allow-insecure-loopback-auth"], env=env, stdout=log, stderr=log, start_new_session=True)
        proof.wait_http(origin + "/_termdeck/healthz")
        cookies = proof.pair_cookies(origin, secret); secret = ""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(args.chromium), headless=True, args=["--disable-dev-shm-usage", "--no-first-run"])
            ctx = browser.new_context(viewport={"width": 1200, "height": 700}); ctx.add_cookies(cookies); ctx.add_init_script(WIRE)
            page = ctx.new_page(); page.set_default_timeout(15000)
            errors = []; page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            def check(name, condition=True):
                if not condition:
                    result["diagnostic"] = {"screen": text(), "wire": page.evaluate("scrollSockets.map(s=>({state:s.readyState,frames:s.frames.length,events:s.events.slice(-4)}))")}
                    raise AssertionError(name)
                result["checks"][name] = True
            def wait(js, arg=None):
                proof.wait_until(lambda: page.evaluate(js, arg), timeout=15, detail=js[:100])
            def text():
                return page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.readBuffer()")
            def first_row():
                rows = re.findall(r"CODEX_SCROLL_ROW_(\d+)", text())
                return min(map(int, rows)) if rows else None
            def wheel(delta):
                box = page.locator("#terminal-xterm .xterm-screen").bounding_box()
                page.mouse.move(box["x"] + 60, box["y"] + 100)
                page.mouse.wheel(0, delta); page.wait_for_timeout(100)
            page.goto(origin + "/scroll/t/" + tabs["Codex"])
            wait("()=>document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('CODEX_SCROLL_ROW_0249')")
            page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            start = first_row()
            result["initial_first_row"] = start
            check("real_codex_alternate_screen", page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.terminal.buffer.active.type==='alternate'"))
            check("application_page_controls_visible", page.get_by_role("button", name="Previous application page").is_visible()
                and not page.locator(".terminal-scroll-position").is_visible())
            wheel(-160)
            proof.wait_until(lambda: first_row() < start, timeout=10, detail="wheel moved actual Codex transcript")
            after = first_row(); check("wheel_moves_conversation_without_prompt_recall", "Ask Codex to do anything" in text())
            page.get_by_role("button", name="Previous application page").click()
            proof.wait_until(lambda: first_row() < after, timeout=10, detail="page button moved Codex transcript")
            check("page_button_moves_actual_codex")
            page.evaluate("window.scrollSocket=scrollSockets.findLast(s=>s.readyState===WebSocket.OPEN)")
            wait("()=>scrollSocket.events.some(e=>e.type==='ack')")
            row = first_row()
            page.evaluate("""() => { window.savedFrame=scrollSocket.frames.findLast(f=>f[0]===3); scrollSocket.send(new Uint8Array(savedFrame)); }""")
            wait("()=>scrollSocket.events.some(e=>e.type==='ack' && e.duplicate===true)")
            check("replay_does_not_scroll_twice", first_row() == row)
            # This deliberate replay bypasses the frontend's in-flight queue.
            # Its unsolicited duplicate acknowledgement is quarantined, so
            # wait for the new socket and explicitly focus before continuing.
            wait("()=>scrollSocket.readyState===WebSocket.CLOSED && scrollSockets.some(s=>s.readyState===WebSocket.OPEN && s!==scrollSocket)")
            wait("()=>['live','viewer'].includes(document.querySelector('#terminal-status').dataset.state)")
            page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            page.evaluate("window.scrollSocket=scrollSockets.findLast(s=>s.readyState===WebSocket.OPEN)")
            result["scroll_trace"] = []
            for _ in range(14):
                wheel(-240)
                result["scroll_trace"].append({"first_row": first_row(), "state": page.evaluate("(()=>{const h=document.querySelector('#terminal-xterm'); return {type:h.termdeckTerminal.terminal.buffer.active.type,overflow:h.dataset.termdeckOverflow,top:h.scrollTop,mouse:h.termdeckTerminal.terminal.modes.mouseTrackingMode,frames:scrollSocket.frames.length}})()")})
            result["top_screen"] = text()
            proof.wait_until(lambda: first_row() == 0, timeout=10, detail="complete Codex transcript top")
            check("complete_conversation_top_reachable", first_row() == 0)
            page.screenshot(path=str(out / "codex-conversation-top.png"))
            for _ in range(14): wheel(240)
            proof.wait_until(lambda: "CODEX_SCROLL_ROW_0249" in text() and "Back to bottom" not in text(), timeout=10, detail="Codex live transcript edge")
            check("conversation_returns_to_latest", "CODEX_SCROLL_ROW_0249" in text() and "Back to bottom" not in text())
            start = first_row()
            page.get_by_role("button", name="Previous application page").click()
            proof.wait_until(lambda: first_row() < start, timeout=10, detail="scroll position before switch")
            row = first_row()
            page.evaluate("id=>document.querySelector(`.workspace-tab-select[data-pane-id='${id}']`).click()", tabs["Other"])
            wait("()=>document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('STALE_SHELL_READY')")
            page.evaluate("id=>document.querySelector(`.workspace-tab-select[data-pane-id='${id}']`).click()", tabs["Codex"])
            check("warm_tab_switch_keeps_app_conversation_position", first_row() == row)
            peer = ctx.new_page(); peer.goto(origin + "/scroll/t/" + tabs["Codex"])
            proof.wait_until(lambda: peer.evaluate("()=>!!document.querySelector('#terminal-xterm')?.termdeckTerminal"), timeout=15, detail="peer ready")
            peer.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='viewer'")
            page.evaluate("window.sentBefore=scrollSocket.frames.length")
            wheel(-160)
            page.get_by_role("button", name="Previous application page").click()
            check("viewer_scroll_never_takes_control", page.evaluate("scrollSocket.frames.length===sentBefore")
                and peer.evaluate("document.querySelector('#terminal-status').dataset.state==='live'"))
            peer_screen = peer.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.readBuffer()")
            page.evaluate("""()=>{const f=new Uint8Array(13);f[0]=3;new DataView(f.buffer).setBigUint64(1,1n);f.set([27,91,53,126],9);scrollSocket.send(f)}""")
            wait("()=>scrollSocket.readyState===WebSocket.CLOSED")
            check("forged_viewer_navigation_denied_by_server", peer.evaluate("document.querySelector('#terminal-status').dataset.state==='live'")
                and peer.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.readBuffer()") == peer_screen)
            wait("()=>scrollSockets.some(s=>s.readyState===WebSocket.OPEN && s!==scrollSocket)")
            wait("()=>['live','viewer'].includes(document.querySelector('#terminal-status').dataset.state)")
            peer.close()
            page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            page.evaluate("window.scrollSocket=scrollSockets.findLast(s=>s.readyState===WebSocket.OPEN)")
            # An invalid page-navigation frame closes this disposable socket.
            page.evaluate("""()=>{const f=new Uint8Array(13); f[0]=3;new DataView(f.buffer).setBigUint64(1,BigInt(scrollSocket.control.next_sequence)); f.set([27,91,65,0],9); scrollSocket.send(f)}""")
            wait("()=>scrollSocket.readyState===WebSocket.CLOSED")
            check("malformed_navigation_rejected")
            wait("()=>scrollSockets.some(s=>s.readyState===WebSocket.OPEN && s!==scrollSocket)")
            # Stop only the fixture's native Codex, then prove stale navigation
            # cannot leak PageUp bytes into its replacement shell-like logger.
            for entry in Path('/proc').iterdir():
                if not entry.name.isdecimal(): continue
                try:
                    if Path(os.readlink(entry / 'cwd')) != root or Path(os.readlink(entry / 'exe')) != codex: continue
                    os.kill(int(entry.name), signal.SIGTERM)
                except (FileNotFoundError, PermissionError, ProcessLookupError): pass
            wait("()=>document.querySelector('#terminal-xterm').termdeckTerminal.readBuffer().includes('STALE_SHELL_READY')")
            page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            page.evaluate("""()=>{window.stale=scrollSockets.findLast(s=>s.readyState===WebSocket.OPEN);window.staleSequence=stale.control.next_sequence;const f=new Uint8Array(13);f[0]=3;new DataView(f.buffer).setBigUint64(1,BigInt(staleSequence));f.set([27,91,53,126],9);stale.send(f)}""")
            wait("()=>stale.events.some(e=>e.type==='ack' && e.sequence===staleSequence)")
            page.wait_for_timeout(200)
            check("stale_alternate_mode_cannot_send_shell_navigation", not (root / "stale.raw").exists() or b"\x1b[5~" not in (root / "stale.raw").read_bytes())
            check("no_separate_history_panel", page.locator(".terminal-history").count() == 0)
            check("no_browser_errors", not errors); result["page_errors"] = errors
            browser.close()
    except Exception as exc:
        result["failure"] = type(exc).__name__ + ": " + str(exc).splitlines()[0][:300]
    finally:
        if server:
            os.killpg(server.pid, signal.SIGTERM); server.wait(timeout=15)
        if tmux:
            subprocess.run([str(tmux), "kill-server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        shutil.rmtree(root)
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return int("failure" in result)


if __name__ == "__main__":
    raise SystemExit(main())
