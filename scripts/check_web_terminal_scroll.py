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
        const p=probes[name],b=p.api.terminal.buffer.active,s=p.host.querySelector('.scrollbar.vertical');
        return {type:b.type,top:b.viewportY,extent:b.baseY,input:p.input,focus:p.focus,
            owner:p.host.dataset.termdeckScrollOwner,maximum:Number(s.getAttribute('aria-valuemax')),
            position:Number(s.getAttribute('aria-valuenow')),hidden:s.getAttribute('aria-hidden')==='true'};
    }""", name)


def wheel(page, y, name="first", x=0):
    box=page.locator(f"#{name} .xterm-screen").bounding_box()
    assert box
    page.mouse.move(box["x"]+40,box["y"]+40)
    page.mouse.wheel(x,y)
    page.wait_for_timeout(100)


def assert_single_scrollback_control(page, name="first"):
    assert page.locator(f"#{name} .scrollbar.vertical").count()==1
    assert page.locator(f"#{name}-shell input[type=range], #{name}-shell .terminal-scroll-controls").count()==0
    page.wait_for_function("name=>Number(probes[name].host.querySelector('.scrollbar.vertical').getAttribute('aria-valuemax'))===probes[name].api.terminal.buffer.active.baseY",arg=name)
    native=page.locator(f"#{name} .scrollbar.vertical")
    assert native.get_attribute('role')=='scrollbar'
    assert native.evaluate("e=>getComputedStyle(e).display")!='none'
    if state(page,name)['extent']:
        page.wait_for_function("name=>Number(getComputedStyle(probes[name].host.querySelector('.scrollbar.vertical')).opacity)===1",arg=name)
        assert native.locator('.slider').evaluate("e=>e.getBoundingClientRect().width")<=5


def drag_history(page, name, bottom):
    bar=page.locator(f'#{name} .scrollbar.vertical')
    track=bar.bounding_box();thumb=bar.locator('.slider').bounding_box()
    assert track and thumb and thumb['height']>0
    x=thumb['x']+thumb['width']/2;y=thumb['y']+thumb['height']/2
    page.mouse.move(x,y);page.mouse.down()
    page.mouse.move(x,track['y']+(track['height'] if bottom else 0),steps=12)
    page.mouse.up();page.wait_for_timeout(60)


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
            for name in ["first", "second"]:
                assert_single_scrollback_control(page, name)
            result["single_visible_scrollback_control"] = True
            wheel(page, -160)
            moved = state(page)
            assert moved["top"] < first["top"] and moved["input"] == [], moved
            assert state(page, "second")["top"] == second["top"], "tabs shared a scroll position"
            slider = page.locator("#first .scrollbar.vertical")
            assert page.locator("#first .terminal-scroll-controls").count() == 0, "scroll controls reduced terminal host width"
            assert slider.is_visible() and slider.is_enabled()
            drag_history(page,"first",False)
            page.wait_for_function("probes.first.api.terminal.buffer.active.viewportY === 0")
            drag_history(page,"first",True)
            page.wait_for_function("probes.first.api.terminal.buffer.active.viewportY === probes.first.api.terminal.buffer.active.baseY")
            result["real_pointer_drag_normal_history"] = True
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
            result["native_proportional_thumb_and_live_control"] = True
            result["multi_tab_isolation"] = True

            # Some pagers and embedded TUIs request mouse input without the
            # alternate screen. Their handle must follow the application's
            # mouse protocol rather than silently selecting browser history.
            write(page, "first", "\x1b[?1000h\x1b[?1006h")
            page.wait_for_timeout(40)
            normal_mouse = state(page)
            assert normal_mouse["owner"] == "application" and not normal_mouse["hidden"], normal_mouse
            assert_single_scrollback_control(page)
            page.evaluate("probes.first.input=[]")
            wheel(page,160);wheel(page,160)
            normal_reports=state(page)['input']
            assert normal_reports and all(x['wheel'] for x in normal_reports),normal_reports
            page.evaluate("probes.first.input=[]")
            page.keyboard.down('Shift');wheel(page,-160);page.keyboard.up('Shift')
            assert state(page)['input']==[] and state(page)['top']<normal_mouse['top']
            write(page, "first", "\x1b[?1000l\x1b[?1006l")
            page.wait_for_timeout(40)
            assert state(page)['owner']=='browser'
            page.evaluate("probes.first.api.terminal.scrollToBottom()")
            result['normal_buffer_application_wheel_and_shift_history']=True

            protocol_cases=[]
            for buffer in ('normal','alternate'):
                write(page,'first','\x1b[?1049h' if buffer=='alternate' else '\x1b[?1049l')
                for tracking in (1000,1002,1003):
                    for encoding in ('legacy','sgr'):
                        write(page,'first','\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l'+f'\x1b[?{tracking}h'+('\x1b[?1006h' if encoding=='sgr' else ''))
                        reports=page.evaluate("""async()=>{
                            const p=probes.first,screen=p.host.querySelector('.xterm-screen');
                            const box=screen.getBoundingClientRect();p.input=[];
                            screen.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,
                              deltaMode:1,deltaY:2,clientX:box.x+40,clientY:box.y+40}));
                            await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
                            return p.input;
                        }""")
                        prefix='1b5b3c3635' if encoding=='sgr' else '1b5b4d61'
                        assert len(reports)==2 and all(x['wheel'] and x['hex'].startswith(prefix) for x in reports),(buffer,tracking,encoding,reports)
                        assert state(page)['owner']=='application'
                        protocol_cases.append({'buffer':buffer,'tracking':tracking,'encoding':encoding})
            write(page,'first','\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1049l')
            result['mouse_protocol_matrix']=protocol_cases

            write(page, "first", "\x1b[?1049h\x1b[2J\x1b[HAlternate screen without mouse support\r\n")
            assert_single_scrollback_control(page)
            page.evaluate("probes.first.input = []")
            wheel(page, -160)
            assert state(page)["input"] == [] and state(page)["hidden"]
            assert page.locator(".terminal-history").count() == 0
            assert page.locator("#first-shell .terminal-scroll-status").is_hidden()
            page.screenshot(path=str(evidence / "alternate-no-history.png"))
            write(page, "first", "\x1b[?9h")
            wheel(page, -160)
            assert state(page)["input"] == [], "X10 wheel fell back to prompt arrows"
            write(page, "first", "\x1b[?9l\x1b[?1000h\x1b[?1006h")
            wheel(page, -160)
            data = state(page)["input"]
            assert data and all(frame["hex"].startswith("1b5b3c3634") and frame["wheel"] for frame in data), data
            result["sgr_mouse_preserved_and_tagged"] = True

            # There is no absolute app offset in a terminal stream. Its native
            # history track stays unavailable while an alternate buffer is active.
            assert state(page)['hidden']
            assert page.locator('.terminal-scroll-controls, .terminal-scroll-position').count()==0
            result['no_invented_application_position_or_controls']=True

            # Preserve fractional trackpad motion, a larger wheel delta, and
            # discard queued input when control or the application mode changes.
            samples = page.evaluate("""async () => {
                const p = probes.first, screen = p.host.querySelector('.xterm-screen');
                const box = screen.getBoundingClientRect();
                const send = delta => screen.dispatchEvent(new WheelEvent('wheel', {
                    bubbles: true, cancelable: true, deltaMode: 1, deltaY: delta,
                    clientX: box.x + 100, clientY: box.y + 100,
                }));
                const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                p.input = []; for(let i=0;i<4;i++) send(-0.25);
                await frames(); const fractional = p.input.length;
                p.input = []; send(-16); await frames(); await frames();
                const distance = p.input.length;
                p.input = []; send(-120); p.mine = false; await frames();
                const viewer = p.input.length; p.mine = true;
                p.input = []; send(-120);
                await new Promise(r => p.api.write('\\x1b[?1000l', r)); await frames();
                const mode = p.input.length;
                await new Promise(r => p.api.write('\\x1b[?1000h', r));
                return {fractional, distance, viewer, mode};
            }""")
            assert samples == {"fractional": 1, "distance": 16, "viewer": 8, "mode": 8}, samples
            result["trackpad_distance_and_pending_input_fences"] = samples

            page.evaluate("probes.first.mine = false; probes.first.input = []")
            page.wait_for_timeout(80)
            assert state(page)["input"] == [], "viewer controls sent mouse input"
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
            page.evaluate("probes.first.input=[]")
            page.wait_for_timeout(80)
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
            assert_single_scrollback_control(page)
            assert page.locator("#first").evaluate("e => getComputedStyle(e).overflowY") == "auto"
            page.evaluate("probes.first.scroll.dispose(); probes.first.scroll.dispose()")
            assert page.locator("#first-shell .terminal-scroll-controls").count() == 0
            assert page.locator("#first .scrollbar.vertical").get_attribute("role") is None
            assert page.locator("#first .scrollbar.vertical").evaluate("e => getComputedStyle(e).display") != "none"
            assert_single_scrollback_control(page, "second")
            result["native_scrollbar_kept_on_dispose"] = True
            page.evaluate("probes.first.api.dispose()")
            wheel(page, -160, "second")
            assert state(page, "second")["top"] < second["top"]
            mutations = page.evaluate("""async () => {
                const p = probes.second;
                p.api.terminal.scrollToBottom();
                let changes = 0;
                const observer = new MutationObserver(records => changes += records.length);
                observer.observe(p.host.querySelector('.scrollbar.vertical'), {attributes: true});
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
                <div class="xterm-shell"><div class="xterm-host"><div class="xterm-scrollable-element">
                <div class="scrollbar vertical visible" style="position:absolute;right:0;height:400px;width:14px"></div></div></div>
                <button class="terminal-scroll-live">↓</button></div>
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
