#!/usr/bin/env python3
"""Prove wheel framing and lease fencing with private tmux and WebSockets."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from check_web_terminal_e2e import (
    IsolatedServer, create_invitation, free_port, pair, sha256_file,
    tmux, wait_until, write_registry, write_t1000_wrapper, write_tmux_wrapper,
)


ROOT = Path(__file__).resolve().parents[1]
PACKET = b"\x1b[<64;2;3M"
LEGACY = b"\x1b[M\x61\x22\x23"
MOTION = b"\x1b[<35;5;3M"


def attach(page, name, pane="td-fixture"):
    return page.evaluate("""({name, pane}) => new Promise((resolve, reject) => {
        window.streams ||= {};
        const record = window.streams[name] = {events: [], closed: false};
        const socket = record.socket = new WebSocket(
            `ws://${location.host}/_termdeck/ws/v1/windows/random/panes/${pane}`,
            'termdeck.terminal.v1');
        const timer = setTimeout(() => reject(new Error('terminal ready timeout')), 10000);
        socket.binaryType = 'arraybuffer';
        socket.addEventListener('open', () => socket.send(JSON.stringify({type:'hello',cols:80,rows:24})));
        socket.addEventListener('message', event => {
            if (typeof event.data !== 'string') return;
            const value = JSON.parse(event.data);
            record.events.push(value);
            if (value.type === 'ready') { clearTimeout(timer); resolve(value); }
        });
        socket.addEventListener('close', () => { record.closed = true; });
        socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('terminal socket failed')); });
    })""", {"name": name, "pane": pane})


def send_frame(page, name, sequence, packet, *, kind=2, result="ack"):
    count = page.evaluate("""({name, sequence, packet, kind}) => {
        const record = streams[name];
        const before = record.events.length;
        const frame = new Uint8Array(9 + packet.length);
        frame[0] = kind;
        new DataView(frame.buffer).setBigUint64(1, BigInt(sequence), false);
        frame.set(packet,9);
        record.socket.send(frame);
        return before;
    }""", {"name": name, "sequence": sequence, "packet": list(packet), "kind": kind})
    page.wait_for_function("""({name,count,result}) => streams[name].events.slice(count).some(e => e.type === result)""",
                           arg={"name": name, "count": count, "result": result}, timeout=10_000)
    return page.evaluate("""({name,count,result}) => streams[name].events.slice(count).find(e => e.type === result)""",
                         {"name": name, "count": count, "result": result})


def current_controller(page):
    return page.evaluate("""async () => {
        const response = await fetch('/_termdeck/api/v1/windows/random/panes/td-fixture/control');
        const value = await response.json(); return value.control || value;
    }""")


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
    run_root = Path(tempfile.mkdtemp(prefix="wheel-backend-", dir=parent))
    socket_name = f"t1000-wheel-{os.getpid()}-{time.time_ns()}"
    names = ["termdeck_wheel_one", "termdeck_wheel_two"]
    database = run_root / "sessions.sqlite3"
    auth_parent = run_root / "auth-parent"
    auth_parent.mkdir(mode=0o700)
    auth = auth_parent / "auth"
    wrapper = run_root / "tmux"
    t1000 = run_root / "t1000"
    probe = run_root / "probe.py"
    probe.write_text("""import os, select, sys, termios, tty
from pathlib import Path
output, mode = map(Path, sys.argv[1:])
fd = sys.stdin.fileno()
tty.setraw(fd)
current = None
while True:
    desired = mode.read_text().strip()
    if desired != current:
        current = desired
        data = b'\\x1b[?1000l\\x1b[?1003l\\x1b[?1006l'
        if desired in ('sgr','legacy','any'): data += b'\\x1b[?1000h'
        if desired in ('sgr','any'): data += b'\\x1b[?1006h'
        if desired == 'any': data += b'\\x1b[?1003h'
        os.write(sys.stdout.fileno(), data + ('MODE:' + desired + '\\r\\n').encode())
    if select.select([fd], [], [], 0.025)[0]:
        with output.open('ab') as handle: handle.write(os.read(fd,4096))
""", encoding="utf-8")
    inputs = [run_root / f"input-{i}.bin" for i in range(2)]
    modes = [run_root / f"mode-{i}.txt" for i in range(2)]
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    result = {"schema": "termdeck.wheel-backend.v1", "binary_sha256": sha256_file(binary)}
    server = None
    try:
        write_tmux_wrapper(wrapper, socket_name)
        write_t1000_wrapper(t1000, socket_name)
        for i, name in enumerate(names):
            inputs[i].write_bytes(b"")
            modes[i].write_text("sgr")
            command = shlex.join(["/usr/bin/python3", str(probe), str(inputs[i]), str(modes[i])])
            tmux(socket_name, "-f", "/dev/null", "new-session", "-d", "-s", name,
                 "-x", "80", "-y", "24", command)
        write_registry(database, names[0], ROOT)
        with sqlite3.connect(database) as connection:
            connection.execute("""INSERT INTO sessions
                SELECT 'td-second', 'Second probe', cwd, ?, status, window_id,
                    window_title, 1, visible, pinned, last_opened_at, pane_target FROM sessions""", (names[1],))
        first_invite = create_invitation(binary, auth, "Wheel controller")
        second_invite = create_invitation(binary, auth, "Wheel viewer")
        command = [str(binary), "--db", str(database), "--listen", f"127.0.0.1:{port}",
                   "--refresh-ms", "250", "--max-stale-ms", "10000", "--tmux", str(wrapper),
                   "--tmux-timeout-ms", "3000", "--t1000", str(t1000),
                   "--host-id", "host-e2e", "--owner-id", "owner-e2e",
                   "--auth-dir", str(auth), "--allow-insecure-loopback-auth"]
        server = IsolatedServer(command, ROOT, origin, evidence)
        server.start()
        cookies = [pair(origin, first_invite), pair(origin, second_invite)]

        def mode(value):
            modes[0].write_text(value)
            wanted = {"off": "0\t0\t0", "legacy": "1\t0\t0", "sgr": "1\t1\t0", "any": "1\t1\t1"}[value]
            wait_until(lambda: tmux(socket_name, "display-message", "-p", "-t", names[0],
                                    "#{mouse_any_flag}\t#{mouse_sgr_flag}\t#{mouse_all_flag}").stdout.strip() == wanted)

        def delivered(value):
            wait_until(lambda: inputs[0].read_bytes() == value)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
                                                   args=["--disable-dev-shm-usage", "--no-first-run"])
            try:
                contexts = [browser.new_context() for _ in range(2)]
                pages = []
                for context, value in zip(contexts, cookies):
                    context.add_cookies(value)
                    page = context.new_page()
                    page.goto(origin + "/_termdeck/healthz")
                    pages.append(page)
                controller, viewer = pages
                ready = attach(controller, "writer")
                assert ready["control"]["mine"], ready
                owner = ready["control"]["controller_device_id"]
                watching = attach(viewer, "viewer")
                assert not watching["control"]["mine"], watching
                mode("sgr")
                assert send_frame(controller, "writer", 1, PACKET)["duplicate"] is False
                delivered(PACKET)
                assert send_frame(controller, "writer", 1, PACKET)["duplicate"] is True
                time.sleep(0.05)
                assert inputs[0].read_bytes() == PACKET
                mode("off")
                send_frame(controller, "writer", 2, PACKET)
                assert inputs[0].read_bytes() == PACKET, "mouse-off wheel reached the child"
                mode("legacy")
                send_frame(controller, "writer", 3, PACKET)
                assert inputs[0].read_bytes() == PACKET, "stale SGR wheel reached a legacy child"
                send_frame(controller, "writer", 4, LEGACY)
                delivered(PACKET + LEGACY)
                mode("sgr")
                send_frame(controller, "writer", 5, LEGACY)
                assert inputs[0].read_bytes() == PACKET + LEGACY, "stale legacy wheel reached an SGR child"
                result["mouse_mode_and_encoding_guards"] = True
                result["duplicate_wheel_delivered_once"] = True
                mode("any")
                send_frame(controller, "writer", 6, MOTION, kind=1)
                delivered(PACKET + LEGACY + MOTION)
                duplicate_motion = send_frame(controller, "writer", 6, MOTION, kind=1)
                assert duplicate_motion["duplicate"]
                expected = PACKET + LEGACY + MOTION
                result["controller_motion_preserved_and_not_replayed"] = True

                batch = PACKET * 8
                assert send_frame(controller, "writer", 7, batch)["duplicate"] is False
                expected += batch
                delivered(expected)
                assert send_frame(controller, "writer", 7, batch)["duplicate"] is True
                controller.wait_for_timeout(50)
                assert inputs[0].read_bytes() == expected
                mode("off")
                send_frame(controller, "writer", 8, batch)
                assert inputs[0].read_bytes() == expected
                mode("legacy")
                send_frame(controller, "writer", 9, batch)
                assert inputs[0].read_bytes() == expected
                send_frame(controller, "writer", 10, LEGACY * 8)
                expected += LEGACY * 8
                delivered(expected)
                mode("any")
                result["bounded_batch_delivered_once_and_mode_fenced"] = True
                for index, invalid in enumerate((PACKET * 9, PACKET + LEGACY, PACKET + b"typed", PACKET + PACKET[:-1])):
                    name = f"invalid-batch-{index}"
                    attached = attach(controller, name)
                    answer = send_frame(controller, name, 1, invalid, result="error")
                    assert answer["code"] == "invalid-client-message", answer
                    assert inputs[0].read_bytes() == expected
                result["oversized_mixed_and_truncated_batches_rejected"] = True

                denied = send_frame(viewer, "viewer", 1, PACKET, result="error")
                assert denied["code"] == "controller-changed", denied
                assert current_controller(controller)["controller_device_id"] == owner
                assert inputs[0].read_bytes() == expected
                hover = attach(viewer, "hover")
                assert not hover["control"]["mine"]
                no_hover_claim = send_frame(viewer, "hover", 1, MOTION + MOTION, kind=1, result="error")
                assert no_hover_claim["code"] == "controller-changed", no_hover_claim
                assert current_controller(controller)["controller_device_id"] == owner
                assert inputs[0].read_bytes() == expected
                result["pure_motion_batch_cannot_claim_control"] = True
                reconnect = attach(viewer, "reconnect")
                assert not reconnect["control"]["mine"]
                invalid = send_frame(viewer, "reconnect", 1, b"\x1b[A", result="error")
                assert invalid["code"] == "invalid-client-message", invalid
                assert current_controller(controller)["controller_device_id"] == owner
                result["viewer_and_reconnect_never_take_control"] = True
                result["malformed_wheel_never_reaches_child"] = True

                second = attach(viewer, "other-pane", "td-second")
                assert second["control"]["mine"]
                send_frame(viewer, "other-pane", 1, PACKET)
                wait_until(lambda: inputs[1].read_bytes() == PACKET)
                assert inputs[0].read_bytes() == expected
                result["multi_pane_isolation"] = True

                takeover = attach(viewer, "intent")
                assert not takeover["control"]["mine"]
                viewer.evaluate("streams.intent.socket.send(JSON.stringify({type:'focus'}))")
                viewer.wait_for_function("() => streams.intent.events.some(e => e.type === 'control' && e.control.mine)")
                new_owner = current_controller(viewer)["controller_device_id"]
                assert new_owner != owner
                stale = send_frame(controller, "writer", 11, PACKET, result="error")
                assert stale["code"] == "controller-changed", stale
                assert current_controller(viewer)["controller_device_id"] == new_owner
                assert inputs[0].read_bytes() == expected
                send_frame(viewer, "intent", 1, PACKET)
                delivered(expected + PACKET)
                result["stale_controller_wheel_cannot_reclaim"] = True
                assert tmux(socket_name, "display-message", "-p", "-t", names[0], "#{pane_in_mode}").stdout.strip() == "0"
                result["tmux_copy_mode_unchanged"] = True

                # Finally exercise the shipped app's real event-to-frame path.
                # A module-only proof would miss input batching or frame-kind
                # mistakes in app.js.
                frames = [[], []]
                for index, page in enumerate(pages):
                    page.on("websocket", lambda socket, i=index: socket.on("framesent", lambda data: frames[i].append(data)))
                    page.goto(origin + "/random/t/td-fixture")
                    page.locator("#terminal-xterm .xterm-screen").wait_for()
                    page.wait_for_function("() => ['live','viewer'].includes(document.body.dataset.termdeckConnection)")
                controller.evaluate("document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
                controller.wait_for_function("() => document.body.dataset.termdeckConnection === 'live'")
                real_owner = current_controller(controller)["controller_device_id"]
                rect = controller.locator("#terminal-xterm .xterm-screen").bounding_box()
                assert rect
                controller.mouse.move(rect["x"] + 40, rect["y"] + 40)
                controller.mouse.wheel(0, -160)
                deadline = time.monotonic() + 10
                while not any(isinstance(frame, bytes) and frame[0] == 2 for frame in frames[0]):
                    if time.monotonic() > deadline:
                        raise RuntimeError("app.js did not send a wheel frame")
                    controller.wait_for_timeout(50)
                wheel_frame = next(frame for frame in frames[0] if isinstance(frame, bytes) and frame[0] == 2)
                assert wheel_frame[9:].startswith(b"\x1b[<64;")
                wait_until(lambda: inputs[0].read_bytes().endswith(wheel_frame[9:]))
                controller.evaluate("""() => {
                    const api = document.querySelector('#terminal-xterm').termdeckTerminal;
                    const screen = api.terminal.element.querySelector('.xterm-screen');
                    const rect = screen.getBoundingClientRect();
                    // The second paste is batched behind the first in-flight
                    // frame. Wheel must flush it or drop without stranding it.
                    api.paste('BEFORE_WHEEL_A');
                    api.paste('BEFORE_WHEEL_B');
                    screen.dispatchEvent(new WheelEvent('wheel', {bubbles:true,cancelable:true,
                        deltaY:-160,clientX:rect.x+40,clientY:rect.y+40}));
                }""")
                wait_until(lambda: b"BEFORE_WHEEL_ABEFORE_WHEEL_B" in inputs[0].read_bytes())
                assert inputs[0].read_bytes().count(b"BEFORE_WHEEL_A") == 1
                assert inputs[0].read_bytes().count(b"BEFORE_WHEEL_B") == 1
                controller.wait_for_timeout(150)
                result["typing_then_wheel_preserves_pending_keyboard_input"] = True
                before_viewer = inputs[0].read_bytes()
                rect = viewer.locator("#terminal-xterm .xterm-screen").bounding_box()
                assert rect
                viewer.mouse.move(rect["x"] + 40, rect["y"] + 40)
                viewer.mouse.wheel(0, -160)
                viewer.wait_for_timeout(250)
                assert current_controller(controller)["controller_device_id"] == real_owner
                assert inputs[0].read_bytes() == before_viewer, "viewer hover or wheel reached the child through app.js"
                result["actual_app_wheel_uses_fenced_frame"] = True
                result["actual_app_viewer_hover_and_wheel_preserve_control"] = True
                result["ok"] = True
            finally:
                browser.close()
    finally:
        if server:
            server.stop()
        # This name is allocated solely by this test; never use the default socket.
        try:
            tmux(socket_name, "kill-server")
        except Exception:
            pass
    (evidence / "wheel-backend.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
