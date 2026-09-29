#!/usr/bin/env python3
"""Exercise real xterm appearance controls in a disposable Brave page."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"


def attach(page):
    page.evaluate("""() => {
        window.T1000Backdrop = () => {};
        window.T1000Files = {
            configure: () => {}, id: () => '0'.repeat(32),
            management: async () => ({}),
        };
        window.T1000Management.configure({});
        const host = document.querySelector('#terminal');
        const settings = window.T1000UI.preferences();
        window.term = window.TermDeckTerminal.createTerminal(host, {...settings, webgl: false});
        window.term.fitGrid(160, 45);
        const tools = window.T1000UI.attachWorkspace({
            root: document.querySelector('#root'),
            toolbar: document.querySelector('#toolbar'),
            menu: document.querySelector('#menu'),
            getPanes: () => [{id:'one',title:'one',cwd:'/home/arch',status:'running'}],
            selectPane: () => {}, getTerminal: () => window.term,
            newTab: () => {}, runAction: () => {}, loadHidden: async () => [],
            restoreHidden: async () => {}, getWindow: () => ({id:'window'}),
            refresh: async () => {}, snapshot: async () => ({windows:[]}),
            appPath: path => '/t1000' + path,
        });
        document.querySelector('#menu').append(...tools.menuItems());
        tools.updateTabs([{id:'one',title:'one',cwd:'/home/arch',status:'running'}], 'one');
    }""")


def state(page):
    return page.evaluate("""() => ({
        fontSize: window.term.terminal.options.fontSize,
        cursorStyle: window.term.terminal.options.cursorStyle,
        palette: window.term.terminal.options.theme.background,
        cursorColor: window.term.terminal.options.theme.cursor,
        fontFamily: window.term.terminal.options.fontFamily,
        blink: window.term.terminal.options.cursorBlink,
        scrollback: window.term.terminal.options.scrollback,
        overflow: document.querySelector('#terminal').dataset.termdeckOverflow,
        clientWidth: document.querySelector('#terminal').clientWidth,
        scrollWidth: document.querySelector('#terminal').scrollWidth,
        columns: window.term.terminal.cols,
        rows: window.term.terminal.rows,
        saved: JSON.parse(localStorage.getItem('t1000.appearance') || '{}'),
    })""")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/brave", headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.route("https://termdeck.test/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body="""<!doctype html><html><body>
                <div id='root'><div class='workspace-title'>Fixture</div>
                <div class='workspace-tabbar'></div>
                <div class='workspace-shell'><div id='terminal'></div></div></div>
                <div id='toolbar'></div><details id='menu'><summary>Actions</summary></details>
                </body></html>"""))
            page.goto("https://termdeck.test/")
            page.add_style_tag(path=str(ASSETS / "app.css"))
            page.add_style_tag(path=str(ASSETS / "terminal.css"))
            page.add_style_tag(content="""
                #terminal {width: min(1185px, calc(100vw - 16px));
                    height: min(685px, calc(100vh - 16px)); overflow: auto;}
                #terminal .xterm {height: max-content; width: max-content;}
            """)
            for asset in ("terminal.js", "codex-palette.js", "workspace-ui.js"):
                page.add_script_tag(path=str(ASSETS / asset))
            attach(page)
            menu_names = page.locator("#menu button").all_text_contents()
            assert menu_names.count("Terminal appearance") == 1, menu_names
            assert "Codex accounts" in menu_names and "Client files" in menu_names, menu_names
            assert "Settings" not in menu_names, menu_names
            page.locator("#menu summary").focus()
            page.keyboard.press("Enter")
            page.get_by_role("button", name="Terminal appearance").click()
            page.get_by_label("Terminal text size (px)").fill("24")
            page.get_by_label("Terminal text size (px)").dispatch_event("change")
            page.get_by_label("Cursor shape").select_option("bar")
            page.get_by_label("Scrollback lines").fill("1200")
            page.get_by_label("Scrollback lines").dispatch_event("change")
            page.get_by_label("Terminal palette").select_option("nord")
            page.get_by_label("Terminal font", exact=True).select_option("system")
            page.get_by_label("Cursor color").fill("#abcdef")
            page.get_by_label("Cursor color").dispatch_event("change")
            page.get_by_label("Blink cursor").uncheck()
            applied = state(page)
            assert applied["fontSize"] == 24 and applied["saved"]["fontSize"] == 24, applied
            assert applied["columns"] == 160 and applied["rows"] == 45, applied
            assert applied["overflow"] == "true", applied
            assert applied["cursorStyle"] == "bar" and applied["scrollback"] == 1200, applied
            assert applied["fontFamily"] == "monospace" and not applied["blink"], applied
            assert applied["palette"] == "#2e3440", applied
            assert applied["cursorColor"] == "#abcdef", applied
            switched = page.evaluate("""() => {
                const host = document.createElement('div');
                host.style.cssText = 'width:760px;height:400px;overflow:auto';
                document.body.append(host);
                const next = window.TermDeckTerminal.createTerminal(host,
                    {...window.T1000UI.preferences(), webgl: false});
                next.fitGrid(160, 45);
                const result = {fontSize: next.terminal.options.fontSize,
                    palette: next.terminal.options.theme.background,
                    cursor: next.terminal.options.cursorStyle,
                    scrollback: next.terminal.options.scrollback};
                next.dispose(); host.remove();
                return result;
            }""")
            assert switched == {"fontSize": 24, "palette": "#2e3440",
                                "cursor": "bar", "scrollback": 1200}, switched
            page.get_by_label("Terminal text size (px)").fill("9")
            page.get_by_label("Terminal text size (px)").dispatch_event("change")
            assert state(page)["fontSize"] == 24, "invalid size changed terminal"
            page.get_by_label("Terminal text size (px)").fill("24")
            page.get_by_role("button", name="Close").first.click()
            page.set_viewport_size({"width": 760, "height": 640})
            narrow = state(page)
            assert narrow["fontSize"] == 24 and narrow["scrollWidth"] > narrow["clientWidth"], narrow
            page.reload()
            page.add_style_tag(path=str(ASSETS / "app.css"))
            page.add_style_tag(path=str(ASSETS / "terminal.css"))
            page.add_style_tag(content="""
                #terminal {width: min(1185px, calc(100vw - 16px));
                    height: min(685px, calc(100vh - 16px)); overflow: auto;}
                #terminal .xterm {height: max-content; width: max-content;}
            """)
            for asset in ("terminal.js", "codex-palette.js", "workspace-ui.js"):
                page.add_script_tag(path=str(ASSETS / asset))
            attach(page)
            restored = state(page)
            assert restored["fontSize"] == 24 and restored["cursorStyle"] == "bar", restored
            assert restored["palette"] == "#2e3440" and restored["scrollback"] == 1200, restored
            assert restored["cursorColor"] == "#abcdef" and restored["fontFamily"] == "monospace", restored
            page.locator("#menu summary").click()
            page.get_by_role("button", name="Terminal appearance").click()
            page.get_by_role("button", name="Reset to defaults").click()
            reset = state(page)
            assert reset["fontSize"] == 13 and reset["cursorStyle"] == "block", reset
            assert reset["scrollback"] == 20000 and reset["saved"]["fontSize"] == 13, reset
            print(json.dumps({"ok": True, "applied": applied, "restored": restored, "reset": reset}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
