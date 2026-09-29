#!/usr/bin/env python3
"""Disposable browser check for the Codex directory picker; no live API calls."""

from pathlib import Path
import json

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "web/crates/termdeck-web-server/assets/workspace-ui.js"
PALETTE_SCRIPT = SCRIPT.with_name("codex-palette.js")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/brave", headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page()
            page.set_content("""<!doctype html><html><body>
                <div id="root"><div class="workspace-shell"></div></div>
                <div id="toolbar"></div><details id="menu"><summary>Actions</summary></details>
            </body></html>""")
            page.add_script_tag(path=str(PALETTE_SCRIPT))
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("""() => {
                window.calls = [];
                window.failBetaOnce = true;
                window.T1000Backdrop = () => {};
                window.T1000Management.workspace = () => ({menuItems: () => []});
                let nextId = 0;
                window.T1000Files = {
                    id: () => String(++nextId).padStart(32, '0'),
                    management: async request => {
                        window.calls.push(request);
                        if (request.operation === 'launch-options') return {
                            ok: true, policy_revision: 'fixture-policy',
                            runtimes: [{id: 'codex', label: 'Codex'}],
                            directory: '/projects', directories: [
                                {name: 'alpha', path: '/projects/alpha'},
                                {name: 'beta', path: '/projects/beta'},
                            ],
                        };
                        if (request.workspace_path.endsWith('beta') && window.failBetaOnce) {
                            window.failBetaOnce = false;
                            throw new Error('temporary fixture failure');
                        }
                        return {ok: true, status: 'started', window_id: request.workspace_path.endsWith('alpha') ? 'one' : 'two'};
                    },
                };
                const root = document.querySelector('#root');
                const tools = window.T1000UI.attachWorkspace({
                    root, toolbar: document.querySelector('#toolbar'), menu: document.querySelector('#menu'),
                    getPanes: () => [{id: 'current', title: 'current', cwd: '/projects', status: 'running'}],
                    selectPane: () => {}, getTerminal: () => null, newTab: () => {}, runAction: () => {},
                    loadHidden: async () => [], restoreHidden: async () => {},
                    getWindow: () => ({id: 'current-window'}), refresh: async () => {},
                    snapshot: async () => ({windows: [
                        {id: 'one', path: '/one'}, {id: 'two', path: '/two'},
                    ]}),
                    appPath: path => '/t1000' + path,
                });
                const menu = document.querySelector('#menu');
                menu.append(...tools.menuItems());
                tools.updateTabs([{id: 'current', title: 'current', cwd: '/projects', status: 'running'}], 'current');
            }""")
            page.locator("#menu summary").click()
            page.get_by_role("button", name="Launch Codex in directories").click()
            picker = page.get_by_role("combobox", name="Directory")
            picker.wait_for()
            page.get_by_text("2 host directories available").wait_for()
            assert picker.locator("option").all_text_contents() == [
                "All immediate subdirectories (2)", "alpha", "beta",
            ]
            assert picker.input_value() == "0", "All must be an explicit choice"
            picker.select_option("all")
            page.get_by_role("textbox", name="Codex prompt").fill("Review code; $(not a command)")
            page.get_by_role("button", name="Launch Codex", exact=True).click()
            page.get_by_text("1 created, 1 failed.", exact=False).wait_for()
            page.get_by_role("button", name="Retry unresolved").click()
            page.get_by_text("2 Codex windows created.").wait_for()
            calls = page.evaluate("window.calls")
            launches = [item for item in calls if item["operation"] == "create-window"]
            assert len(launches) == 3, calls
            assert [item["workspace_path"] for item in launches] == ["/projects/alpha", "/projects/beta", "/projects/beta"]
            assert len({item["request_id"] for item in launches}) == 2
            assert launches[1]["request_id"] == launches[2]["request_id"]
            assert all(item["runtime"] == "codex" and item["yolo"] is True
                       and item["task"] == "Review code; $(not a command)" for item in launches)
            assert page.get_by_role("link", name="Open window").count() == 2
            assert page.get_by_role("button", name="Retry unresolved").is_disabled()
            print(json.dumps({"ok": True, "requests": len(launches), "unique_launches": 2,
                              "retry_reused_id": True}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
