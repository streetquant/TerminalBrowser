#!/usr/bin/env python3
"""Exercise browser scroll routing in disposable real xterm instances.

This check emits no input to a TermDeck session. It uses the built browser
assets and captures xterm's input bytes, scroll positions, and visible controls.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"


def write(page, name, data):
    page.evaluate("""({name, data}) => new Promise(resolve => {
        window.probes[name].api.write(data, resolve);
    })""", {"name": name, "data": data})


def state(page, name="first"):
    return page.evaluate("""name => {
        const p = window.probes[name], buffer = p.api.terminal.buffer.active;
        return {type: buffer.type, top: buffer.viewportY, extent: buffer.baseY,
            input: p.input, focus: p.focus, owner: p.host.dataset.termdeckScrollOwner,
            maximum: Number(p.shell.querySelector('input').max),
            disabled: p.shell.querySelector('input').disabled};
    }""", name)


def wheel(page, y, name="first", x=0):
    screen = page.locator(f"#{name} .xterm-screen").bounding_box()
    assert screen
    page.mouse.move(screen["x"] + 40, screen["y"] + 40)
    page.mouse.wheel(x, y)
    # Real wheel dispatch arrives on the next compositor tick.
    page.wait_for_timeout(80)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--browser", default="/usr/bin/brave")
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema": "termdeck.browser-scroll.v1"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=args.browser, headless=True,
            args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_content("""<!doctype html><html><body>
                <div id="first-shell" class="xterm-shell"><div id="first" class="xterm-host"></div></div>
                <div id="second-shell" class="xterm-shell"><div id="second" class="xterm-host"></div></div></body></html>""")
            page.add_style_tag(path=str(ASSETS / "terminal.css"))
            page.add_style_tag(path=str(ASSETS / "app.css"))
            page.add_style_tag(content="body {margin:16px} .xterm-shell {position:relative;inset:auto;width:940px;height:350px;margin-bottom:12px}")
            page.add_script_tag(path=str(ASSETS / "terminal.js"))
            page.add_script_tag(path=str(ASSETS / "terminal-scroll.js"))
            page.evaluate("""() => {
                window.probes = {};
                for (const name of ['first', 'second']) {
                    const host = document.getElementById(name);
                    const p = {host, shell: host.parentElement, input: [], focus: 0, mine: true};
                    const api = p.api = TermDeckTerminal.createTerminal(host, {
                        webgl: false, cursorBlink: false, scrollback: 500,
                    });
                    p.scroll = T1000TerminalScroll.install(host, api, {isController: () => p.mine});
                    api.onData(bytes => p.input.push({hex: [...bytes].map(x => x.toString(16).padStart(2,'0')).join(''),
                        wheel: p.scroll.isWheelInput()}));
                    host.addEventListener('pointerdown', () => p.focus++);
                    probes[name] = p;
                }
            }""")
            for name in ["first", "second"]:
                write(page, name, "\r\n".join(f"{name} saved history line {i:04d}" for i in range(200)) + "\r\n")
            first = state(page)
            second = state(page, "second")
            assert first["extent"] > 100 and first["top"] == first["extent"], first
            wheel(page, -160)
            moved = state(page)
            assert moved["top"] < first["top"] and moved["input"] == [], moved
            assert state(page, "second")["top"] == second["top"], "tabs shared a scroll position"
            slider = page.locator("#first-shell .terminal-scroll-position")
            assert page.locator("#first .terminal-scroll-controls").count() == 0, "scroll controls reduced terminal host width"
            assert slider.is_visible() and slider.is_enabled()
            slider.focus()
            page.keyboard.press("Home")
            page.wait_for_function("probes.first.api.terminal.buffer.active.viewportY === 0")
            assert state(page)["focus"] == 0 and state(page)["input"] == []
            page.screenshot(path=str(evidence / "primary-history.png"))
            page.keyboard.press("End")
            page.wait_for_function("probes.first.api.terminal.buffer.active.viewportY === probes.first.api.terminal.buffer.active.baseY")
            wheel(page, 500000)
            assert state(page)["top"] == first["extent"] and state(page)["input"] == []
            wheel(page, -160)
            page.get_by_role("button", name="Return to live output").first.click()
            assert state(page)["focus"] == 0, "scroll control claimed terminal focus"
            result["primary_local_scroll_and_bounds"] = True
            result["visible_slider_and_live_control"] = True
            result["multi_tab_isolation"] = True

            write(page, "first", "\x1b[?1049h\x1b[2J\x1b[HAlternate screen without mouse support\r\n")
            page.evaluate("probes.first.input = []")
            wheel(page, -160)
            assert state(page)["input"] == [] and state(page)["disabled"]
            page.locator("#first-shell .terminal-scroll-status").filter(has_text="No scrollback available").wait_for()
            page.screenshot(path=str(evidence / "alternate-no-history.png"))
            write(page, "first", "\x1b[?9h")
            wheel(page, -160)
            assert state(page)["input"] == [], "X10 wheel fell back to prompt arrows"
            write(page, "first", "\x1b[?9l\x1b[?1000h\x1b[?1006h")
            wheel(page, -160)
            data = state(page)["input"]
            assert data and all(frame["hex"].startswith("1b5b3c3634") and frame["wheel"] for frame in data), data
            result["sgr_mouse_preserved_and_tagged"] = True

            page.evaluate("probes.first.mine = false; probes.first.input = []")
            wheel(page, -160)
            assert state(page)["input"] == [] and state(page)["focus"] == 0
            page.locator("#first-shell .terminal-scroll-status").filter(has_text="Click the terminal").wait_for()
            write(page, "first", "\x1b[?1003h")
            page.mouse.move(990, 780)
            wheel(page, -160)
            assert state(page)["input"] == [], "passive viewer hover emitted an app mouse report before wheel"
            page.evaluate("probes.first.mine = true")
            page.mouse.move(990, 780)
            wheel(page, -160)
            controller_reports = state(page)["input"]
            assert any(frame["hex"].startswith("1b5b3c3335") and not frame["wheel"] for frame in controller_reports), controller_reports
            write(page, "first", "\x1b[?1003l\x1b[?1000h")
            page.evaluate("probes.first.input = []")
            result["viewer_hover_blocked_and_controller_motion_preserved"] = True
            page.keyboard.down("Shift")
            wheel(page, -160)
            page.keyboard.up("Shift")
            assert state(page)["input"] == [], "Shift-wheel reached app input"
            write(page, "first", "\x1b[?1006l")
            wheel(page, 160)
            legacy = state(page)["input"]
            assert legacy and all(frame["hex"].startswith("1b5b4d61") and frame["wheel"] for frame in legacy), legacy
            result["legacy_mouse_preserved_and_tagged"] = True
            result["viewer_wheel_never_claims_or_sends"] = True
            result["alternate_and_x10_never_send_prompt_arrows"] = True

            write(page, "first", "\x1b[?1000l\x1b[?1049l")
            page.evaluate("probes.first.input = []; probes.first.mine = false")
            wheel(page, -160)
            assert state(page)["top"] < first["extent"] and state(page)["input"] == []
            page.evaluate("probes.first.api.reset()")
            write(page, "first", "Fresh reconnect replay\r\n")
            wheel(page, -160)
            empty = state(page)
            assert empty["extent"] == 0 and empty["maximum"] == 0 and empty["input"] == [], empty
            assert empty["owner"] == "browser"
            result["reconnect_and_no_history_reset"] = True
            page.evaluate("probes.first.api.fitGrid(120,60); probes.first.host.scrollTop = 0")
            wheel(page, 160)
            assert page.evaluate("probes.first.host.scrollTop") > 0, "viewer could not inspect an overflowing canonical grid"
            assert state(page)["input"] == []
            result["viewer_grid_overflow_scrolls_locally"] = True
            page.evaluate("probes.first.scroll.dispose(); probes.first.scroll.dispose(); probes.first.api.dispose()")
            assert page.locator("#first-shell .terminal-scroll-controls").count() == 0
            wheel(page, -160, "second")
            assert state(page, "second")["top"] < second["top"]
            mutations = page.evaluate("""async () => {
                const p = probes.second;
                p.api.terminal.scrollToBottom();
                let changes = 0;
                const observer = new MutationObserver(records => changes += records.length);
                observer.observe(p.shell.querySelector('.terminal-scroll-position'), {attributes: true});
                await new Promise(resolve => p.api.write('burst line\\r\\n'.repeat(10000), resolve));
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                observer.disconnect();
                return changes;
            }""")
            assert mutations < 100, f"scroll controls mutated {mutations} times during one burst"
            result["scroll_control_mutations_for_10000_line_burst"] = mutations
            # The mobile Tabs pane overlays the terminal. Its controls must
            # remain above the terminal's positioned scroll slider.
            page.set_viewport_size({"width": 320, "height": 568})
            page.set_content('''<body class="workspace-page"><div class="workspace-shell"
                data-sidebar="open" style="width:320px;height:400px">
                <div class="xterm-shell"><div class="terminal-scroll-controls">
                <input class="terminal-scroll-position" type="range" disabled></div></div>
                <aside class="workspace-sidebar" aria-label="Tabs pane">
                <div class="workspace-sidebar-heading"><h2>Tabs</h2>
                <button aria-label="Move Tabs pane to left"
                onclick="document.querySelector('.workspace-shell').dataset.sidebarSide='left'">Move</button>
                <button aria-label="Hide Tabs pane" onclick="window.sidebarClicks++">×</button>
                </div></aside></div><script>window.sidebarClicks=0</script></body>''')
            page.add_style_tag(path=str(ASSETS / "app.css"))
            pane = page.get_by_role("complementary", name="Tabs pane")
            pane.get_by_role("button", name="Hide Tabs pane").click(timeout=3000)
            assert page.evaluate("window.sidebarClicks") == 1
            pane.get_by_role("button", name="Move Tabs pane to left").click(timeout=3000)
            assert page.locator(".workspace-shell").get_attribute("data-sidebar-side") == "left"
            pane.get_by_role("button", name="Hide Tabs pane").click(timeout=3000)
            assert page.evaluate("window.sidebarClicks") == 2
            page.screenshot(path=str(evidence / "mobile-sidebar-above-scroll-controls.png"))
            result["mobile_sidebar_above_scroll_controls"] = True
            assert not errors, errors
            result["dispose_isolated"] = True
            result["page_errors"] = errors
            result["ok"] = True
        finally:
            browser.close()
    (evidence / "browser-scroll.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
