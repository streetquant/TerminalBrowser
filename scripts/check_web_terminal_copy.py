#!/usr/bin/env python3
"""Disposable browser check of TermDeck's user-selected terminal copy flow."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "web/crates/termdeck-web-server/assets/terminal-copy.js"


def actual_clipboard(browser):
    """Use Brave's real clipboard API from a secure loopback origin."""
    asset = SCRIPT.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            extra = {"/terminal.js": SCRIPT.with_name("terminal.js"), "/terminal.css": SCRIPT.with_name("terminal.css")}
            content = extra[self.path].read_bytes() if self.path in extra else asset if self.path == "/terminal-copy.js" else (
                b'<div id="terminal"></div><script src="/terminal-copy.js"></script>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/css" if self.path.endswith(".css") else "text/javascript" if self.path.endswith(".js") else "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        result = page.evaluate("""async () => {
            const expected = 'TermDeck actual browser clipboard probe';
            const host = document.querySelector('#terminal');
            window.T1000TerminalCopy.install(host, {selection: () => expected});
            host.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, button:0, clientX:1, clientY:1}));
            host.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, button:0, clientX:30, clientY:1}));
            await new Promise(resolve => setTimeout(resolve, 250));
            return {secure: isSecureContext, exact: await navigator.clipboard.readText() === expected,
                notice: document.body.textContent.includes('Selection copied')};
        }""")
        assert result == {"secure": True, "exact": True, "notice": True}, result
        # Reload to remove the synthetic terminal API, then select real xterm
        # text using actual mouse events and read the browser clipboard back.
        page.reload()
        page.add_style_tag(url=f"http://127.0.0.1:{server.server_port}/terminal.css")
        page.add_style_tag(content="#terminal {width:720px;height:240px;margin:32px}")
        page.add_script_tag(url=f"http://127.0.0.1:{server.server_port}/terminal.js")
        page.evaluate("""() => new Promise(resolve => {
            const host = document.querySelector('#terminal');
            window.api = TermDeckTerminal.createTerminal(host, {fontSize: 18});
            window.disposeCopy = T1000TerminalCopy.install(host, window.api);
            window.api.write('ALPHA BETA GAMMA\\r\\nSECOND LINE SELECTION\\r\\n', resolve);
        })""")
        screen = page.locator('.xterm-screen').bounding_box()
        dimensions = page.evaluate("({cols: api.terminal.cols, rows: api.terminal.rows})")
        assert screen
        cell = screen['width'] / dimensions['cols']
        row = screen['height'] / dimensions['rows']
        x, y = screen['x'], screen['y'] + row / 2
        cases = []
        for name, clicks in (("double-click", 2), ("triple-click", 3)):
            page.evaluate("api.terminal.clearSelection(); navigator.clipboard.writeText('before')")
            page.mouse.click(x + cell * 7, y, click_count=clicks)
            page.wait_for_function("async () => api.selection().length > 0 && await navigator.clipboard.readText() === api.selection()")
            selected = page.evaluate("api.selection()")
            assert 'BETA' in selected, (name, selected)
            cases.append({"gesture": name, "exact": True, "selection": selected})
        page.evaluate("api.terminal.clearSelection(); navigator.clipboard.writeText('before')")
        page.mouse.move(x + cell / 2, y)
        page.mouse.down()
        page.mouse.move(x + cell * 5.5, y, steps=8)
        page.mouse.up()
        page.wait_for_function("async () => api.selection().length > 0 && await navigator.clipboard.readText() === api.selection()")
        cases.append({"gesture": "drag", "exact": True, "selection": page.evaluate("api.selection()")})
        page.evaluate("api.terminal.clearSelection(); navigator.clipboard.writeText('before')")
        page.mouse.move(x + cell / 2, y)
        page.mouse.down()
        page.mouse.move(x + screen['width'] + 20, y + row, steps=12)
        page.mouse.up()
        page.wait_for_function("async () => api.selection().length > 0 && await navigator.clipboard.readText() === api.selection()")
        cases.append({"gesture": "outside-release", "exact": True, "selection": page.evaluate("api.selection()")})
        assert page.get_by_text("Selection copied", exact=True).is_visible()
        page.evaluate("disposeCopy(); api.dispose()")
        context.close()
        return {**result, "real_xterm": cases}
    finally:
        server.shutdown()
        server.server_close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/brave", headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page()
            page.set_content('<div id="terminal"></div>')
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("""() => {
                window.copied = [];
                window.selected = '';
                window.denyCopy = false;
                Object.defineProperty(window, 'isSecureContext', {configurable: true, value: true});
                Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                    writeText: async value => {
                        if (window.denyCopy) throw new Error('denied');
                        window.copied.push(value);
                    },
                }});
                window.host = document.querySelector('#terminal');
                window.disposeCopy = window.T1000TerminalCopy.install(window.host, {
                    selection: () => window.selected,
                });
                window.drag = () => {
                    window.host.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, button: 0, clientX: 1, clientY: 1}));
                    window.host.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, button: 0, clientX: 20, clientY: 1}));
                };
            }""")
            page.evaluate("window.selected = 'secret selected by user'; window.host.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true,button:0,clientX:1,clientY:1})); window.host.dispatchEvent(new PointerEvent('pointerup', {bubbles:true,button:0,clientX:1,clientY:1}));")
            assert page.evaluate("window.copied") == [], "ordinary click must not copy"
            page.evaluate("window.drag()")
            page.get_by_text("Selection copied").wait_for()
            assert page.evaluate("window.copied") == ["secret selected by user"]
            page.evaluate("window.selected = ''; window.drag()")
            assert len(page.evaluate("window.copied")) == 1, "empty selection must not copy"
            page.evaluate("window.selected = 'next selection'; window.denyCopy = true; window.drag()")
            page.get_by_text("Clipboard permission denied", exact=False).wait_for()
            assert page.get_by_role("button", name="Copy selection").is_visible()
            assert len(page.evaluate("window.copied")) == 1, "denied write must not be reported as copied"
            page.evaluate("window.denyCopy = false")
            page.get_by_role("button", name="Copy selection").click()
            page.get_by_text("Selection copied").wait_for()
            assert page.evaluate("window.copied") == ["secret selected by user", "next selection"]
            page.evaluate("window.disposeCopy(); window.drag()")
            assert len(page.evaluate("window.copied")) == 2, "disposed terminal must not copy"
            print(json.dumps({"ok": True, "positive_copies": 2, "empty_selection_copies": 0,
                              "denial_reported": True, "disposed_copy_blocked": True,
                              "actual_clipboard": actual_clipboard(browser)}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
