#!/usr/bin/env python3
"""Check captured alternate-screen history in a disposable browser terminal."""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ASSETS = Path(__file__).resolve().parents[1] / "web/crates/termdeck-web-server/assets"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/brave", headless=True,
            args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = browser.new_page(viewport={"width": 900, "height": 600})
            page.set_content('''<!doctype html><main id="termdeck-app" data-termdeck-base="/t1000">
                <div class="xterm-shell" style="position:relative;width:800px;height:350px">
                <div id="host" class="xterm-host"></div></div></main>''')
            page.add_style_tag(path=str(ASSETS / "terminal.css"))
            page.add_style_tag(path=str(ASSETS / "app.css"))
            page.add_script_tag(path=str(ASSETS / "terminal.js"))
            page.add_script_tag(path=str(ASSETS / "terminal-scroll.js"))
            page.evaluate("""() => {
                const host = document.getElementById('host');
                const api = TermDeckTerminal.createTerminal(host, {
                    webgl: false, cursorBlink: false, scrollback: 500,
                });
                api.fitGrid(80, 15);
                window.input = [];
                window.captures = 0;
                window.payload = {schema: 'termdeck.terminal-capture.v1', pane_id: '%1',
                    text: Array.from({length: 300}, (_, i) => `saved line ${i}`).join('\\n'),
                    truncated: false};
                window.fetch = async url => {
                    captures++;
                    if (url !== '/t1000/_termdeck/api/v1/windows/demo/panes/%251/capture') {
                        throw new Error(`unexpected URL ${url}`);
                    }
                    return new Response(JSON.stringify(payload), {
                        status: 200, headers: {'content-type': 'application/json'},
                    });
                };
                window.captureFetch = window.fetch;
                const scroll = T1000TerminalScroll.install(host, api,
                    {isController: () => false, capture: ['demo', '%1']});
                api.onData(bytes => input.push(bytes));
                window.probe = {api, scroll, host};
            }""")
            page.evaluate("""() => new Promise(resolve =>
                probe.api.write('\\x1b[?1049h\\x1b[2J\\x1b[HApp screen', resolve))""")
            page.evaluate("""() => document.querySelector('#host .xterm-screen').dispatchEvent(
                new WheelEvent('wheel', {bubbles: true, cancelable: true, deltaY: -180}))""")
            page.wait_for_function("document.querySelector('.terminal-history-text').textContent.includes('saved line 299')")
            state = page.evaluate("""() => {
                const history = document.querySelector('.terminal-history');
                const text = document.querySelector('.terminal-history-text');
                const slider = document.querySelector('.terminal-scroll-position');
                return {open: !history.hidden, focused: document.activeElement === text,
                    captures, overflow: text.scrollHeight > text.clientHeight,
                    top: text.scrollTop, sliderMax: Number(slider.max), input: input.length,
                    owner: probe.host.dataset.termdeckScrollOwner};
            }""")
            assert state["open"] and state["focused"] and state["overflow"] and state["captures"] == 1, state
            assert state["sliderMax"] > 1 and state["input"] == 0 and state["owner"] == "capture", state
            page.evaluate("""() => {
                const slider = document.querySelector('.terminal-scroll-position');
                slider.value = '0'; slider.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
            assert page.evaluate("document.querySelector('.terminal-history-text').scrollTop") == 0
            page.evaluate("document.querySelector('.terminal-history-heading button').click()")
            assert page.evaluate("document.querySelector('.terminal-history').hidden")
            assert page.evaluate("document.activeElement === document.querySelector('.terminal-scroll-position')")
            page.evaluate("""() => {
                const slider = document.querySelector('.terminal-scroll-position');
                slider.value = '0'; slider.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
            page.wait_for_function("captures === 2")
            assert page.evaluate("input.length") == 0
            page.evaluate("document.querySelector('.terminal-history-heading button').click()")
            page.evaluate("payload.schema = 'malformed'")
            page.evaluate("""() => document.querySelector('#host .xterm-screen').dispatchEvent(
                new WheelEvent('wheel', {bubbles: true, cancelable: true, deltaY: -180}))""")
            page.wait_for_function("document.querySelector('.terminal-history-text').textContent.includes('unavailable')")
            assert page.evaluate("input.length") == 0
            page.evaluate("document.querySelector('.terminal-history-heading button').click()")
            page.evaluate("window.fetch = async () => new Response('{}', {status: 401})")
            page.evaluate("""() => document.querySelector('#host .xterm-screen').dispatchEvent(
                new WheelEvent('wheel', {bubbles: true, cancelable: true, deltaY: -180}))""")
            page.wait_for_function("document.querySelector('.terminal-history-text').textContent.includes('unavailable')")
            assert page.evaluate("input.length") == 0
            page.evaluate("document.querySelector('.terminal-history-heading button').click()")
            page.evaluate("window.fetch = captureFetch; payload.schema = 'termdeck.terminal-capture.v1'; payload.text = ''")
            page.evaluate("""() => document.querySelector('#host .xterm-screen').dispatchEvent(
                new WheelEvent('wheel', {bubbles: true, cancelable: true, deltaY: -180}))""")
            page.wait_for_function("document.querySelector('.terminal-history-text').textContent.includes('No terminal history')")
            assert page.evaluate("input.length") == 0
            page.evaluate("probe.scroll.dispose(); probe.api.dispose()")
            print(json.dumps({"ok": True, "viewer_input": 0, "wheel_and_slider": True,
                              "malformed_rejected": True, "unauthorized_rejected": True,
                              "empty_history_explicit": True}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
