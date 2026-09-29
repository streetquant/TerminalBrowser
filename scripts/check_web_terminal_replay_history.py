#!/usr/bin/env python3
"""Check retained tmux history in the live browser terminal with private fixtures."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from check_web_terminal_e2e import (
    IsolatedServer,
    create_invitation,
    free_port,
    pair,
    sha256_file,
    tmux,
    wait_until,
    write_registry,
    write_t1000_wrapper,
    write_tmux_wrapper,
)


ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"HISTORY-ROW-(\d{4})")


def browser_state(page):
    return page.evaluate("""() => {
        const api = document.querySelector('#terminal-xterm')?.termdeckTerminal;
        const buffer = api?.terminal?.buffer?.normal;
        if (!buffer) return null;
        const markers = [];
        for (let index = 0; index < buffer.length; index += 1) {
            const text = buffer.getLine(index)?.translateToString(true) || '';
            for (const match of text.matchAll(/HISTORY-ROW-(\\d{4})/g)) {
                markers.push(Number(match[1]));
            }
        }
        return {markers, length: buffer.length, ybase: buffer.baseY,
            ydisp: buffer.viewportY, alternate: api.terminal.buffer.active !== buffer,
            hasHistoryPanel: !!document.querySelector('[data-terminal-history-panel]')};
    }""")


def assert_complete(page, socket_name, session):
    page.wait_for_function("() => ['live', 'viewer'].includes(document.body.dataset.termdeckConnection)", timeout=20_000)
    page.wait_for_function("""() => {
        const api = document.querySelector('#terminal-xterm')?.termdeckTerminal;
        return api?.terminal?.buffer?.normal?.length > 1_800;
    }""", timeout=20_000)
    captured = tmux(socket_name, "capture-pane", "-pq", "-t", session, "-S", "-4000").stdout
    expected = [int(value) for value in MARKER.findall(captured)]
    observed = browser_state(page)
    assert len(expected) >= 1_800, f"private tmux retained only {len(expected)} rows"
    assert observed and not observed["alternate"] and not observed["hasHistoryPanel"], observed
    assert expected == observed["markers"], {
        "expected_count": len(expected), "observed_count": len(observed["markers"]),
        "expected_edges": expected[:2] + expected[-2:],
        "observed_edges": observed["markers"][:2] + observed["markers"][-2:],
        "missing": sorted(set(expected) - set(observed["markers"]))[:20],
        "extra": sorted(set(observed["markers"]) - set(expected))[:20],
    }
    page.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.terminal.scrollToTop()")
    page.wait_for_function("""() => document.querySelector('#terminal-xterm')
        ?.termdeckTerminal?.terminal?.buffer?.normal?.viewportY === 0""")
    assert browser_state(page)["ydisp"] == 0
    page.wait_for_function("""() => {
        const terminal = document.querySelector('#terminal-xterm')?.termdeckTerminal?.terminal;
        const slider = document.querySelector('.terminal-scroll-position');
        return terminal && slider && !slider.disabled
            && Number(slider.max) === terminal.buffer.normal.baseY
            && Number(slider.value) === 0;
    }""")
    return {"retained": len(expected), "oldest": expected[0], "newest": expected[-1],
            "buffer_length": observed["length"], "scroll_base": observed["ybase"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--browser", default="/usr/bin/brave")
    args = parser.parse_args()
    binary = args.binary.resolve()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    parent = ROOT / ".ai-bridge/tmp"
    parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="replay-history-", dir=parent))
    socket_name = f"t1000-replay-{os.getpid()}-{time.time_ns()}"
    session = "termdeck_replay_fixture"
    server = None
    result = {"schema": "termdeck.replay-history.v1", "binary_sha256": sha256_file(binary)}
    try:
        probe = run_root / "history.py"
        trigger = run_root / "trigger"
        probe.write_text("""import sys, time
from pathlib import Path
trigger = Path(sys.argv[1])
for index in range(2300):
    print(f'HISTORY-ROW-{index:04} ' + ('x' * 80), flush=True)
seen = ''
while True:
    value = trigger.read_text() if trigger.exists() else ''
    if value and value != seen:
        seen = value
        if value == 'LONG-LINE':
            print('LONG-LINE-' + ('y' * 300000), flush=True)
            print('LONG-END', flush=True)
        else:
            print('LIVE-' + value, flush=True)
    time.sleep(0.02)
""", encoding="utf-8")
        wrapper = run_root / "tmux"
        t1000 = run_root / "t1000"
        database = run_root / "sessions.sqlite3"
        auth_parent = run_root / "auth-parent"
        auth_parent.mkdir(mode=0o700)
        auth = auth_parent / "auth"
        write_tmux_wrapper(wrapper, socket_name)
        write_t1000_wrapper(t1000, socket_name)
        command = shlex.join(["/usr/bin/python3", "-u", str(probe), str(trigger)])
        tmux(socket_name, "-f", "/dev/null", "new-session", "-d", "-s", session,
             "-x", "181", "-y", "49", command)
        wait_until(lambda: int(tmux(socket_name, "display-message", "-p", "-t", session,
                                    "#{history_size}").stdout.strip()) >= 1_800)
        write_registry(database, session, ROOT)
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        invitation = create_invitation(binary, auth, "Replay fixture")
        server = IsolatedServer([
            str(binary), "--db", str(database), "--listen", f"127.0.0.1:{port}",
            "--refresh-ms", "250", "--max-stale-ms", "10000", "--tmux", str(wrapper),
            "--tmux-timeout-ms", "5000", "--t1000", str(t1000),
            "--host-id", "host-e2e", "--owner-id", "owner-e2e", "--auth-dir", str(auth),
            "--allow-insecure-loopback-auth",
        ], ROOT, origin, evidence)
        server.start()
        cookies = pair(origin, invitation)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
                                                  args=["--disable-dev-shm-usage", "--no-first-run"])
            try:
                context = browser.new_context(viewport={"width": 1456, "height": 985})
                context.add_cookies(cookies)
                page = context.new_page()
                started = time.monotonic()
                page.goto(origin + "/random/t/td-fixture", wait_until="domcontentloaded")
                result["initial"] = assert_complete(page, socket_name, session)
                result["initial"]["attach_ms"] = round((time.monotonic() - started) * 1000, 1)
                trigger.write_text("AFTER-ATTACH", encoding="utf-8")
                page.wait_for_function("""() => document.querySelector('#terminal-xterm')
                    ?.termdeckTerminal?.readBuffer()?.includes('LIVE-AFTER-ATTACH')""", timeout=10_000)
                started = time.monotonic()
                page.reload(wait_until="domcontentloaded")
                result["reconnect"] = assert_complete(page, socket_name, session)
                result["reconnect"]["attach_ms"] = round((time.monotonic() - started) * 1000, 1)
                trigger.write_text("AFTER-RELOAD", encoding="utf-8")
                page.wait_for_function("""() => document.querySelector('#terminal-xterm')
                    ?.termdeckTerminal?.readBuffer()?.includes('LIVE-AFTER-RELOAD')""", timeout=10_000)
                result["live_output_after_attach_and_reconnect"] = True
                trigger.write_text("LONG-LINE", encoding="utf-8")
                page.wait_for_function("""() => document.querySelector('#terminal-xterm')
                    ?.termdeckTerminal?.readBuffer()?.includes('LONG-END')""", timeout=10_000)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("""() => ['live', 'viewer'].includes(document.body.dataset.termdeckConnection)
                    && document.querySelector('#terminal-xterm')
                        ?.termdeckTerminal?.readBuffer()?.includes('LONG-END')""", timeout=20_000)
                result["joined_line_above_old_256k_limit_replayed"] = True
                result["ok"] = True
                context.close()
            finally:
                browser.close()
    finally:
        if server is not None:
            server.stop()
        tmux(socket_name, "kill-server")
        shutil.rmtree(run_root)
    (evidence / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
