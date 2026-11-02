#!/usr/bin/env python3
"""Exercise the saved-tab availability boundary in a disposable browser.

The complete app and terminal-session assets run against synthetic snapshots and
an owned fake WebSocket. No TermDeck endpoint, credentials or user pane is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"

STUBS = """() => {
  window.fixtureSockets = [];
  class FixtureSocket extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    constructor(url, protocol) {
      super(); this.url = url; this.protocol = protocol; this.readyState = 0;
      this.bufferedAmount = 0; this.sent = []; this.closes = [];
      fixtureSockets.push(this);
    }
    send(value) { this.sent.push(value); }
    open() { this.readyState = 1; this.dispatchEvent(new Event('open')); }
    event(value) { this.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(value)})); }
    output(text) { this.dispatchEvent(new MessageEvent('message', {data: new TextEncoder().encode(text).buffer})); }
    close(code = 1000, reason = '') {
      if (this.readyState === 3) return;
      this.readyState = 3; this.closes.push({code, reason});
      this.dispatchEvent(new CloseEvent('close', {code, reason}));
    }
  }
  window.WebSocket = FixtureSocket;
  window.T1000UI = {
    preferences: () => ({webgl: false, cursorBlink: false, fontSize: 14}),
    attachWorkspace: () => ({menuItems: () => [], updateTabs: () => {}}),
    renderHome: () => { throw new Error('unexpected fixture home page'); },
  };
  window.T1000Management = {configure: () => {}};
  window.fixtureReady = (index, mine = false) => {
    const socket = fixtureSockets[index]; socket.open();
    socket.event({type:'ready',geometry:{cols:80,rows:24},control:{
      schema:'termdeck.terminal-control.v1', mine, epoch:1, next_sequence:1, indeterminate:false,
    }});
  };
  window.fixtureView = () => {
    const host = document.getElementById('terminal-xterm');
    const buffer = host?.termdeckTerminal?.terminal.buffer.active;
    const lines = buffer ? Array.from({length:buffer.length}, (_, i) => buffer.getLine(i)?.translateToString() || '') : [];
    return {sockets:fixtureSockets.length,
      state:document.getElementById('terminal-status')?.dataset.state,
      label:document.getElementById('terminal-status')?.textContent,
      overlay:document.getElementById('terminal-overlay')?.textContent,
      overlayHidden:document.getElementById('terminal-overlay')?.hidden,
      ownedBufferRetained:lines.some(line => line.includes('OWNED-FIXTURE-BUFFER')),
      binaryFrames:fixtureSockets.map(s => s.sent.filter(x => typeof x !== 'string').length),
      closes:fixtureSockets.map(s => s.closes.length),
      tabs:document.querySelectorAll('[role=tab]').length,
    };
  };
}"""


def descendants(pid):
    found = set()
    todo = [pid]
    while todo:
        current = todo.pop()
        try:
            children = [int(value) for value in Path(f"/proc/{current}/task/{current}/children").read_text().split()]
        except (FileNotFoundError, ProcessLookupError):
            continue
        for child in children:
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def generation(pid):
    root = Path(f"/proc/{pid}")
    try:
        raw = (root / "stat").read_text()
        return {"pid": pid, "uid": root.stat().st_uid,
                "start_ticks": int(raw[raw.rfind(")") + 2:].split()[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--app", type=Path, default=ASSETS / "app.js")
    parser.add_argument("--session", type=Path, default=ASSETS / "terminal-session.js")
    parser.add_argument("--browser", default="/usr/bin/brave")
    parser.add_argument("--expect-baseline-gap", action="store_true")
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = evidence / "t"
    temporary.mkdir(mode=0o700)
    # Playwright and Chromium both keep private profiles in this short owned root.
    os.environ["TMPDIR"] = str(temporary)
    result = {"schema": "termdeck.browser-terminal-recovery.v1", "fixture_only": True,
              "production_endpoint_or_native_called": False,
              "user_terminal_input_or_buffer_read": False,
              "assets": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.app, args.session)},
              "controls": [], "first_failure": None}
    current = {"status": "recovery-pending", "target": None, "revision": 1}
    requests = []
    owned = []
    errors = []
    before_children = descendants(os.getpid())

    def snapshot():
        return {"schema": "termdeck.window-snapshot.v1", "revision": current["revision"],
                "generated_at_unix_ms": current["revision"],
                "window": {"id": "window-fixture", "name": "Owned fixture", "slug": "fixture", "path": "/fixture",
                           "active_pane_id": "pane-fixture", "panes": [{"id": "pane-fixture", "session_id": "session-fixture",
                            "title": "Owned fixture", "cwd": "/fixture/project", "status": current["status"],
                            "pinned": False, "automatic_title": False, "controller": None, "tmux_target": current["target"]}]}}

    def route(request):
        url = request.request.url
        requests.append({"method": request.request.method, "path": url.partition("termdeck-fixture.invalid")[2]})
        if url == "http://termdeck-fixture.invalid/fixture/t/pane-fixture":
            request.fulfill(status=200, content_type="text/html", body='''<!doctype html><html><body><main id="termdeck-app"
              data-termdeck-page="terminal" data-termdeck-slug="fixture" data-termdeck-pane="pane-fixture"></main></body></html>''')
        elif url == "http://termdeck-fixture.invalid/_termdeck/api/v1/windows/fixture" and request.request.method == "GET":
            request.fulfill(status=200, content_type="application/json", body=json.dumps(snapshot()))
        else:
            request.abort()

    def view(page):
        return page.evaluate("fixtureView()")

    def refresh(page, status, target):
        current.update(status=status, target=target, revision=current["revision"] + 1)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        wanted = current["revision"]
        page.wait_for_function("revision => document.querySelector('#termdeck-app').dataset.termdeckState === 'ready'", arg=wanted)
        # The existing finite refresh timer observes the fresh synthetic response.
        page.wait_for_timeout(230)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
                env={**os.environ, "TMPDIR": str(temporary)},
                args=["--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run", "--no-default-browser-check"])
            try:
                page = browser.new_page(viewport={"width": 1100, "height": 800})
                page.set_default_timeout(5000)
                page.route("**/*", route)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://termdeck-fixture.invalid/fixture/t/pane-fixture")
                page.add_style_tag(path=str(ASSETS / "terminal.css"))
                page.add_style_tag(path=str(ASSETS / "app.css"))
                page.evaluate(STUBS)
                for name in ("terminal.js", "terminal-scroll.js", "terminal-copy.js"):
                    page.add_script_tag(path=str(ASSETS / name))
                page.add_script_tag(path=str(args.session))
                page.add_script_tag(path=str(args.app))
                page.wait_for_function("document.getElementById('terminal-xterm')?.termdeckTerminal && document.getElementById('termdeck-app').dataset.termdeckState === 'ready'")
                owned = [g for pid in descendants(os.getpid()) - before_children if (g := generation(pid))]
                if args.expect_baseline_gap:
                    initial = view(page)
                    assert initial["sockets"] == 1, initial
                    page.evaluate("fixtureSockets[0].open(); fixtureSockets[0].event({type:'error', message:'terminal stream I/O failed: Connection refused (os error 111)', recoverable:true}); fixtureSockets[0].close(4001, 'owned fixture failure')")
                    page.wait_for_timeout(280)
                    stale = view(page)
                    assert "Connection refused" in stale["overlay"], stale
                    result["controls"].append("exact-installed-baseline-pending-tab-retains-E111-and-attempts-stream")
                else:
                    page.wait_for_timeout(650)
                    state = view(page)
                    assert state["sockets"] == 0 and state["state"] == "recovery" and "waiting for recovery" in state["overlay"], state
                    result["controls"].append("pending-tab-never-opens-or-retries-terminal-stream")
                    refresh(page, "running", "fixture:0.0")
                    page.wait_for_function("fixtureSockets.length === 1")
                    page.evaluate("fixtureReady(0, true); fixtureSockets[0].output('OWNED-FIXTURE-BUFFER\\r\\n')")
                    page.wait_for_function("fixtureView().ownedBufferRetained")
                    assert view(page)["overlayHidden"]
                    result["controls"].append("fresh-running-target-connects-once-and-ready-clears-recovery-overlay")
                    page.evaluate("document.getElementById('terminal-xterm').termdeckTerminal.terminal.paste('OWNED-FIXTURE-INPUT')")
                    page.wait_for_function("fixtureView().binaryFrames[0] >= 1")
                    page.evaluate("fixtureSockets[0].event({type:'error', message:'terminal stream I/O failed: Connection refused (os error 111)', recoverable:true})")
                    refresh(page, "recovery-pending", None)
                    pending = view(page)
                    assert pending["sockets"] == 1 and pending["closes"][0] == 1 and pending["ownedBufferRetained"], pending
                    assert "Connection refused" not in pending["overlay"] and "not be replayed" in pending["overlay"], pending
                    result["controls"].append("running-to-pending-closes-generation-preserves-rendered-buffer-discards-input")
                    page.evaluate("fixtureSockets[0].event({type:'ready', geometry:{cols:80,rows:24}, control:{schema:'termdeck.terminal-control.v1',mine:true,epoch:2,next_sequence:1}}); fixtureSockets[0].event({type:'error',message:'LATE-OLD-GENERATION',recoverable:true}); fixtureSockets[0].output('LATE-OLD-OUTPUT'); window.dispatchEvent(new Event('online')); window.dispatchEvent(new Event('pageshow'))")
                    page.wait_for_timeout(650)
                    assert view(page) == pending, view(page)
                    result["controls"].append("late-old-generation-and-online-pageshow-cannot-reattach-blocked-tab")
                    refresh(page, "running", "fixture:0.0")
                    page.wait_for_function("fixtureSockets.length === 2")
                    page.evaluate("fixtureReady(1)")
                    refresh(page, "running", "fixture:0.0")
                    restored = view(page)
                    assert restored["sockets"] == 2 and restored["binaryFrames"][1] == 0 and restored["ownedBufferRetained"] and restored["overlayHidden"], restored
                    result["controls"].append("duplicate-refresh-does-not-reconnect-or-replay-old-input")
                    refresh(page, "running", "fixture:0.1")
                    page.wait_for_function("fixtureSockets.length === 3")
                    replaced = view(page)
                    assert replaced["closes"][1] == 1 and replaced["ownedBufferRetained"] and replaced["binaryFrames"][2] == 0, replaced
                    page.evaluate("fixtureSockets[1].event({type:'error',message:'OLD-TARGET',recoverable:true}); fixtureSockets[1].output('OLD-TARGET-OUTPUT'); fixtureReady(2)")
                    refresh(page, "running", "fixture:0.1")
                    assert view(page)["sockets"] == 3 and view(page)["overlayHidden"], view(page)
                    result["controls"].append("running-target-change-retires-old-generation-preserves-buffer-and-connects-once")
                    refresh(page, "running", None)
                    targetless = view(page)
                    assert targetless["sockets"] == 3 and targetless["closes"][2] == 1 and "no live terminal" in targetless["overlay"], targetless
                    result["controls"].append("running-flag-without-current-target-does-not-connect")
                    refresh(page, "stopped", None)
                    stopped = view(page)
                    assert stopped["sockets"] == 3 and "explicitly relaunch" in stopped["overlay"], stopped
                    assert page.locator('[data-action="rename"]').count() == 1
                    assert page.locator('[data-action="kill"]').count() == 1
                    assert not any(request["method"] != "GET" for request in requests)
                    result["controls"].append("stopped-tab-keeps-manual-relaunch-controls-and-never-launches-automatically")
                    assert not errors, errors
            finally:
                browser.close()
        result["owned_process_generations"] = owned
        result["owned_process_generations_gone"] = all(generation(item["pid"]) != item for item in owned)
        assert result["owned_process_generations_gone"]
        result["page_errors"] = errors
        result["requests"] = requests
        result["pass_count"] = len(result["controls"])
        result["ok"] = True
    except BaseException as error:
        result["ok"] = False
        result["first_failure"] = {"type": type(error).__name__, "message": str(error)}
        result["page_errors"] = errors
        raise
    finally:
        path = evidence / "qualification.json"
        path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"ok": result["ok"], "pass_count": result["pass_count"], "receipt": str(path)}))


if __name__ == "__main__":
    main()
