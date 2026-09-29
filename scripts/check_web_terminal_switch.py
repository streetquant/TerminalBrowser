#!/usr/bin/env python3
"""Exercise recent-tab reuse against disposable terminals and real Chromium.

The browser, registry, credentials and tmux server belong to this test. It never
uses production terminal input or edits a session database directly.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

import check_web_terminal_browser as proof

WIRE = r"""(() => {
  const Native = window.WebSocket;
  window.switchSockets = []; window.switchWire = [];
  function Socket(...args) {
    const ws = new Native(...args), id = switchSockets.length;
    switchSockets.push(ws);
    const send = ws.send.bind(ws);
    ws.send = value => {
      const message = typeof value === 'string' ? JSON.parse(value) : {type:'input'};
      switchWire.push({id,type:message.type,...(message.type==='hello' ? {cols:message.cols,rows:message.rows} : {})});
      send(value);
    };
    ws.addEventListener('message', e => {
      if (typeof e.data === 'string') {
        const v = JSON.parse(e.data);
        if (v.type === 'control' || v.type === 'ready') ws.control = v.control || v;
      }
    });
    return ws;
  }
  Socket.prototype = Native.prototype; Object.setPrototypeOf(Socket, Native);
  window.WebSocket = Socket;
})();"""

FIXTURE = r'''
import os, sys, signal, tty
from pathlib import Path
root, label = Path(sys.argv[1]), sys.argv[2]
(root/(label+'.pid')).write_text(str(os.getpid()))
tty.setraw(0)
def update(*_):
    os.write(1, ('\r\nBACKGROUND_'+label+'\r\n\x1b[6n').encode())
signal.signal(signal.SIGUSR1, update)
os.write(1, ''.join('HISTORY_'+label+'_'+str(i)+'\r\n' for i in range(300)).encode())
os.write(1, ('READY_'+label+'\r\n').encode())
while True:
    data=os.read(0,65536)
    if not data: break
    with (root/(label+'.raw')).open('ab',buffering=0) as f: f.write(data)
    os.write(1,data)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, default=Path("/usr/bin/chromium"))
    args = parser.parse_args()
    binary = args.server_binary.resolve()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    runtime = proof.ROOT / ".termdeck/qa"; runtime.mkdir(mode=0o700, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="switch-", dir=runtime))
    result = {"checks": {}, "binary_sha256": proof.sha256_file(binary)}
    server = browser = tmux = None
    env = os.environ.copy()
    try:
        home = root / "home"
        for d in (home / ".config", home / ".local/share", home / ".cache", root / "tmux", root / "logs", root / "auth-parent"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixture = root / "fixture.py"; fixture.write_text(FIXTURE)
        db = root / "sessions.sqlite3"; auth = root / "auth-parent/auth"
        tmux, t1000 = proof.write_wrappers(root / "bin", home=home, tmux_tmpdir=root / "tmux", python=Path(sys.executable))
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
                   XDG_CACHE_HOME=str(home / ".cache"), TMUX_TMPDIR=str(root / "tmux"), PYTHONPATH=str(proof.ROOT),
                   PATH=f"{root / 'bin'}:/usr/local/bin:/usr/bin:/bin", PYTHONNOUSERSITE="1")
        for k in ("TMUX", "TMUX_PANE", "TERMDECK_STATE_ROOT", "TERMDECK_TMUX_RUNTIME", "TERMDECK_TMUX_IDENTITY"):
            env.pop(k, None)
        tabs = {}
        for label in "ABCDEF":
            p = proof.run_json([str(t1000), "new", "--db", str(db), "--cwd", str(root), "--window-id", "win-switch",
                "--window-title", "Switch", "--name", label, "--command", shlex.join([sys.executable, "-u", str(fixture), str(root / "logs"), label])], env=env)
            tabs[label] = p["tabs"][0]["id"]
        origin = f"http://127.0.0.1:{proof.free_port()}"
        secret = proof.invitation(binary, auth, "Switch coordinator")
        log = (out / "server.log").open("wb")
        server = subprocess.Popen([str(binary), "--db", str(db), "--listen", origin[7:], "--refresh-ms", "250",
            "--max-stale-ms", "10000", "--max-connections", "128", "--tmux", str(tmux), "--tmux-timeout-ms", "3000",
            "--t1000", str(t1000), "--auth-dir", str(auth), "--host-id", "host-browser-e2e", "--owner-id", "owner-browser-e2e",
            "--allow-insecure-loopback-auth"], env=env, stdout=log, stderr=log, start_new_session=True)
        proof.wait_http(origin + "/_termdeck/healthz")
        cookies = proof.pair_cookies(origin, secret); secret = ""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(args.chromium), headless=True,
                args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"])
            ctx = browser.new_context(viewport={"width":1440,"height":900}); ctx.add_cookies(cookies); ctx.add_init_script(WIRE)
            page = ctx.new_page(); page.set_default_timeout(15000)
            errors=[]; page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.goto(origin + "/switch/t/" + tabs["A"])
            def check(name, condition=True):
                if not condition: raise AssertionError(name)
                result["checks"][name] = True
            def wait(js, arg=None):
                proof.wait_until(lambda: page.evaluate(js,arg),timeout=15,detail=js[:120])
            def choose(label):
                page.evaluate("id=>document.querySelector(`.workspace-tab-select[data-pane-id='${id}']`).click()", tabs[label])
                wait("marker=>document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes(marker)", "READY_"+label)
                wait("()=>document.querySelector('#terminal-xterm').contains(document.activeElement)")
                wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            wait("()=>document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('READY_A')")
            for label in "BCD":
                page.evaluate("id=>document.querySelector(`.workspace-tab-select[data-pane-id='${id}']`).click()",tabs[label])
            choose("D")
            check("rapid_cold_switch_keeps_admitted_geometry",page.evaluate("switchSockets.filter(s=>s.readyState===WebSocket.OPEN).length<=4 && switchWire.filter(x=>x.type==='hello').every(x=>x.cols>2 && x.rows>2)"))
            choose("A")
            page.evaluate("window.savedA=document.querySelector('#terminal-xterm').termdeckTerminal")
            page.evaluate("savedA.terminal.scrollLines(-20); window.savedScroll=savedA.terminal.buffer.active.viewportY")
            choose("B")
            pid = int((root / "logs/A.pid").read_text()); os.kill(pid, signal.SIGUSR1)
            wait("()=>savedA.readBuffer().includes('BACKGROUND_A')")
            count=page.evaluate("switchSockets.length")
            choose("A")
            check("warm_switch_reuses_renderer_and_websocket", page.evaluate("savedA===document.querySelector('#terminal-xterm').termdeckTerminal && switchSockets.length") == count)
            check("background_output_is_current", "BACKGROUND_A" in page.evaluate("savedA.readBuffer()"))
            # Revisit a scroll position while no new output arrives.
            page.evaluate("savedA.terminal.scrollLines(-20); window.savedScroll=savedA.terminal.buffer.active.viewportY")
            choose("B"); choose("A")
            check("scrollback_position_preserved", page.evaluate("savedA.terminal.buffer.active.viewportY===savedScroll"))
            check("history_preserved", page.evaluate("savedA.readBuffer().includes('HISTORY_A_0')"))
            choose("B")
            page.evaluate("window.backgroundGeometry=[savedA.terminal.cols,savedA.terminal.rows]; window.backgroundWire=switchWire.length")
            page.evaluate("savedA.terminal.input('HIDDEN_INPUT',true); savedA.terminal.focus()")
            time.sleep(.25)
            check("inactive_input_and_focus_cannot_send", page.evaluate("switchWire.slice(backgroundWire).every(x=>x.type!=='focus' && x.type!=='input')"))
            check("inactive_input_never_reaches_process", not (root / "logs/A.raw").exists() or b"HIDDEN_INPUT" not in (root / "logs/A.raw").read_bytes())
            page.set_viewport_size({"width":1100,"height":760}); time.sleep(.3)
            check("inactive_geometry_does_not_shrink", page.evaluate("backgroundGeometry[0]===savedA.terminal.cols && backgroundGeometry[1]===savedA.terminal.rows"))
            choose("A"); check("active_geometry_refits", page.evaluate("savedA.terminal.cols<backgroundGeometry[0]"))
            # Every already connected tab must accept an immediate keystroke in its own process.
            for label in "ABABAB":
                choose(label)
                token = "INPUT_ONLY_"+label+"_"+str(time.monotonic_ns())
                page.keyboard.type(token)
                proof.wait_bytes(root / ("logs/"+label+".raw"), token.encode())
                other = "B" if label=="A" else "A"
            check("input_isolated_"+token, not (root / ("logs/"+other+".raw")).exists() or token.encode() not in (root / ("logs/"+other+".raw")).read_bytes())
            for index,label in enumerate("ABABAB"):
                token="IMMEDIATE_ONLY_"+label+"_"+str(index)
                page.evaluate("""([id,token])=>{
                  document.querySelector(`.workspace-tab-select[data-pane-id='${id}']`).click();
                  document.querySelector('#terminal-xterm').termdeckTerminal.terminal.input(token,true);
                }""",[tabs[label],token])
                proof.wait_bytes(root / ("logs/"+label+".raw"),token.encode())
                other="B" if label=="A" else "A"
                check("immediate_switch_input_isolated_"+str(index),token.encode() not in (root / ("logs/"+other+".raw")).read_bytes())
            for label in "CDE": choose(label)
            check("live_cache_bound_four", page.evaluate("switchSockets.filter(s=>s.readyState===WebSocket.OPEN).length<=4 && document.querySelector('.workspace-shell').dataset.termdeckCachedTerminals==='4'"))
            choose("A")
            check("evicted_tab_reopens_canonical_process", page.evaluate("savedA!==document.querySelector('#terminal-xterm').termdeckTerminal") and int((root / "logs/A.pid").read_text()) == pid)
            check("single_visible_terminal", page.locator("#terminal-xterm").count()==1 and page.locator("#terminal-shell").count()==1)
            csrf=next(c["value"] for c in cookies if c["name"]=="termdeck_csrf")
            headers={"Origin":origin,"Content-Type":"application/json","X-TermDeck-CSRF":csrf,
                     "Sec-Fetch-Site":"same-origin","Sec-Fetch-Mode":"cors"}
            # HTTP management has a different actor from this cached stream.
            # Type in the same JS task as explicit focus, before a heartbeat can
            # repair the stale browser view, and repeat after sequence advances.
            endpoint=origin+"/_termdeck/api/v1/windows/switch/panes/"+tabs["A"]
            for index in range(2):
                takeover=ctx.request.post(endpoint+"/control", data={"takeover":True}, headers=headers)
                check("http_manager_takeover_"+str(index), takeover.status==200)
                token="FOCUS_AFTER_MANAGER_"+str(index)
                page.evaluate("token=>{const t=document.querySelector('#terminal-xterm').termdeckTerminal;t.focus();t.terminal.input(token,true);}",token)
                proof.wait_bytes(root / "logs/A.raw",token.encode())
                check("stale_cached_lease_focus_keeps_immediate_input_"+str(index),
                    (root / "logs/A.raw").read_bytes().count(token.encode())==1
                    and token.encode() not in (root / "logs/B.raw").read_bytes())
            choose("B")
            page.evaluate("localStorage.setItem('t1000.appearance',JSON.stringify({fontSize:17,palette:'dracula',cursorStyle:'bar'}))")
            choose("A")
            check("cached_view_applies_changed_appearance", page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.terminal.options.fontSize===17 && document.querySelector('#terminal-xterm').termdeckTerminal.terminal.options.cursorStyle==='bar'"))
            choose("C"); choose("A")
            endpoint=origin+"/_termdeck/api/v1/windows/switch/panes/"+tabs["C"]+"/tab"
            check("hide_rejects_missing_csrf", ctx.request.post(endpoint,data={"action":"hide"}).status==403)
            response=ctx.request.post(endpoint,data={"action":"hide"},headers=headers)
            check("hide_cached_tab_accepted",response.status==200)
            wait("id=>!document.querySelector(`.workspace-tab[data-pane-id='${id}']`)",tabs["C"])
            wait("id=>switchSockets.filter(s=>s.url.endsWith('/'+id)).every(s=>s.readyState===WebSocket.CLOSED)",tabs["C"])
            check("hidden_cached_stream_closed")
            choose("D"); choose("A")
            endpoint=origin+"/_termdeck/api/v1/windows/switch/panes/"+tabs["D"]+"/tab"
            check("malformed_kill_rejected",ctx.request.post(endpoint,data={"action":"kill","confirm_session_id":tabs["D"]},headers=headers).status==422)
            response=ctx.request.post(endpoint,data={"action":"kill","confirmation":tabs["D"]},headers=headers)
            check("kill_cached_tab_accepted",response.status==200)
            wait("id=>!document.querySelector(`.workspace-tab[data-pane-id='${id}']`)",tabs["D"])
            wait("id=>switchSockets.filter(s=>s.url.endsWith('/'+id)).every(s=>s.readyState===WebSocket.CLOSED)",tabs["D"])
            check("killed_cached_stream_closed")
            # A second authenticated browser takes over A while the first keeps A in its cache.
            choose("B")
            peer=ctx.new_page(); peer.goto(origin + "/switch/t/"+tabs["A"])
            proof.wait_until(lambda: peer.evaluate("()=>!!document.querySelector('#terminal-xterm')?.termdeckTerminal"),timeout=15,detail="peer renderer")
            peer.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            proof.wait_until(lambda: peer.evaluate("()=>document.querySelector('#terminal-status')?.dataset.state==='live'"),timeout=15,detail="peer controller")
            page.evaluate("window.backgroundWire=switchWire.length")
            os.kill(pid,signal.SIGUSR1); time.sleep(.5)
            check("background_output_does_not_take_over_peer", peer.evaluate("()=>document.querySelector('#terminal-status').dataset.state==='live'") and page.evaluate("switchWire.slice(backgroundWire).every(x=>x.type!=='focus' && x.type!=='input')"))
            peer.close(); choose("A")
            # Only the active view reconnects after an offline event; cached views resume lazily.
            page.evaluate("window.dispatchEvent(new Event('offline'))")
            wait("()=>switchSockets.every(s=>s.readyState===WebSocket.CLOSED)")
            page.evaluate("window.dispatchEvent(new Event('online'))")
            wait("()=>['live','viewer'].includes(document.querySelector('#terminal-status').dataset.state)")
            page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
            wait("()=>document.querySelector('#terminal-status').dataset.state==='live'")
            check("offline_reconnect_is_bounded", page.evaluate("switchSockets.filter(s=>s.readyState===WebSocket.OPEN).length===1"))
            choose("B"); choose("A")
            response=ctx.request.post(origin+"/_termdeck/api/v1/logout",data={},headers=headers)
            check("logout_accepted", response.status==200)
            wait("()=>switchSockets.every(s=>s.readyState===WebSocket.CLOSED)")
            check("logout_closes_every_cached_stream")
            check("stale_session_denied", ctx.request.get(origin+"/_termdeck/api/v1/session").status==401)
            check("no_browser_errors", not errors)
            result["page_errors"]=errors
            ctx.close()
            # Revocation must invalidate all open cached streams, not just the visible one.
            revoked_cookies=proof.pair_cookies(origin,proof.invitation(binary,auth,"Cache revocation"))
            revoked=browser.new_context(viewport={"width":1440,"height":900}); revoked.add_cookies(revoked_cookies); revoked.add_init_script(WIRE)
            page=revoked.new_page(); page.goto(origin+"/switch/t/"+tabs["A"])
            wait("()=>document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer().includes('READY_A')")
            choose("B"); choose("A")
            device=revoked.request.get(origin+"/_termdeck/api/v1/session").json()["device_id"]
            revoke_headers={**headers,"X-TermDeck-CSRF":next(c["value"] for c in revoked_cookies if c["name"]=="termdeck_csrf")}
            check("device_revocation_accepted",revoked.request.post(origin+"/_termdeck/api/v1/devices/"+device+"/revoke",data={},headers=revoke_headers).status==200)
            wait("()=>switchSockets.every(s=>s.readyState===WebSocket.CLOSED)")
            check("revocation_closes_every_cached_stream")
            check("revoked_session_denied",revoked.request.get(origin+"/_termdeck/api/v1/session").status==401)
            revoked.close(); browser.close(); browser=None
    except Exception as exc:
        result["failure"] = type(exc).__name__ + ": " + str(exc).splitlines()[0][:300]
        if browser:
            try:
                result["diagnostic"] = page.evaluate("""() => ({path:location.pathname,
                  status:document.querySelector('#terminal-status')?.textContent,
                  overlay:document.querySelector('#terminal-overlay')?.textContent,
                  wire:switchWire.slice(-25), sockets:switchSockets.map(s=>({state:s.readyState,control:s.control}))})""")
            except Exception:
                result["diagnostic"] = "browser already stopped"
    finally:
        # sync_playwright has already closed its browser/driver on exception.
        if server:
            os.killpg(server.pid,signal.SIGTERM); server.wait(timeout=15)
        if tmux:
            subprocess.run([str(tmux),"kill-server"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
        shutil.rmtree(root)
        (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))
    return int("failure" in result)


if __name__ == "__main__":
    raise SystemExit(main())
