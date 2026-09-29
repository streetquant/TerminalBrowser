#!/usr/bin/env python3
"""Browser regression for automatic tab labels using disposable registry/API state.

Loads the repository's actual browser assets and Python rename/move handlers.
Snapshots and terminal transport are fixtures; the Rust state-mirror tests cover
the authoritative tmux projection. No production endpoint or tmux socket is used.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from termdeck.session_store import SessionStore
from termdeck.web_management import handle

ASSETS = ROOT / "web/crates/termdeck-web-server/assets"
ORIGIN = "http://termdeck.test"
BASE = "/t1000"

TRANSPORT_FIXTURE = """(() => {
  localStorage.setItem('t1000.layout', JSON.stringify({tabsPane: true, side: 'right'}));
  class Socket extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    readyState = 0; bufferedAmount = 0;
    send() { throw new Error('Tab-label fixture must not send terminal input'); }
    close() { this.readyState = 3; }
  }
  window.WebSocket = Socket;
})();"""


class Fixture:
    def __init__(self, root: Path):
        self.store = SessionStore(root / "sessions.sqlite3", provision_auth_scopes=False)
        self.store.remember_window("two", "Two")
        automatic = self.store.create("arch", "terminal", "bash", "/home/arch",
                                      automatic_name=True, window_id="one", window_title="One")
        manual = self.store.create("outreach", "terminal", "bash", "/home/arch",
                                   window_id="one", window_title="One", tab_index=1)
        self.ids = (automatic.id, manual.id)
        self.live = {item: "/home/arch" for item in self.ids}
        for item in self.ids:
            self.store.update(item, status="running")
        self.generation = 100
        self.override = None
        self.requests = []
        self.hold = False
        self.pending = []

    def window(self, slug):
        panes = []
        for row in self.store.list(window_id=slug, visible_only=True):
            cwd = self.live[row.id]
            title = (PurePosixPath(cwd).name or "root") if row.name_is_automatic else row.name
            panes.append({"id": row.id, "session_id": row.id, "title": title,
                          "cwd": cwd, "status": "running", "pinned": row.pinned,
                          "automatic_title": row.name_is_automatic, "controller": None})
        return {"id": slug, "name": slug.title(), "slug": slug, "path": "/" + slug,
                "active_pane_id": panes[0]["id"] if panes else None, "panes": panes}

    def envelope(self, slug):
        return {"schema": "termdeck.window-snapshot.v1", "revision": self.generation,
                "revision_text": str(self.generation), "generated_at_unix_ms": self.generation,
                "window": self.window(slug)}

    def snapshot(self):
        windows = [self.window(slug) for slug in ("one", "two")]
        return {"schema": "termdeck.host-snapshot.v1", "revision": self.generation,
                "generated_at_unix_ms": self.generation,
                "total_panes": sum(len(win["panes"]) for win in windows), "windows": windows}

    def respond_snapshot(self, route, slug):
        route.fulfill(json=self.override or self.envelope(slug),
                      headers={"x-termdeck-stale": "1" if self.override else "0"})

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append((request.method, path, request.post_data_json if request.method == "POST" else None))
        if path.startswith(BASE + "/assets/"):
            asset = ASSETS / path.rsplit("/", 1)[-1]
            assert asset.parent == ASSETS and asset.is_file(), path
            route.fulfill(path=asset, content_type="text/css" if asset.suffix == ".css" else "text/javascript")
        elif path == BASE + "/_termdeck/api/v1/snapshot":
            route.fulfill(json=self.snapshot())
        elif path == BASE + "/_termdeck/api/v1/management":
            body = request.post_data_json
            if body["operation"] == "list":
                route.fulfill(json={"ok": True, "rows": [], "total": 0,
                                    "windows": [{"id": item, "name": item.title()} for item in ("one", "two")]})
            else:
                assert body["operation"] == "move", body
                result = handle(self.store, body)
                self.generation += 1
                route.fulfill(json=result)
        elif path.startswith(BASE + "/_termdeck/api/v1/windows/"):
            parts = path[len(BASE + "/_termdeck/api/v1/windows/"):].split("/")
            if len(parts) == 1:
                if self.hold:
                    self.pending.append((route, parts[0]))
                else:
                    self.respond_snapshot(route, parts[0])
            else:
                assert parts[1] == "panes" and parts[3] == "tab", path
                body = request.post_data_json
                assert body["action"] == "rename", body
                handle(self.store, {"operation": "rename", "ids": [parts[2]], "name": body["name"]})
                self.generation += 1
                route.fulfill(json={"ok": True})
        else:
            parts = path.removeprefix(BASE + "/").split("/")
            assert parts[0] in {"one", "two"}, path
            scripts = ["terminal.js", "terminal-copy.js", "terminal-scroll.js", "client-files.js",
                       "accounts.js", "codex-palette.js", "workspace-ui.js", "app.js"]
            route.fulfill(content_type="text/html", body=(
                '<!doctype html><html><head><meta charset="utf-8"><title>Tab fixture</title>'
                f'<link rel="stylesheet" href="{BASE}/assets/app.css">'
                f'<link rel="stylesheet" href="{BASE}/assets/terminal.css"></head><body>'
                '<div id="termdeck-client-notice" hidden></div>'
                f'<main id="termdeck-app" data-termdeck-page="window" data-termdeck-base="{BASE}" '
                f'data-termdeck-slug="{parts[0]}" data-termdeck-pane="{parts[2] if len(parts) == 3 else ""}"></main>'
                + "".join(f'<script src="{BASE}/assets/{name}"></script>' for name in scripts)
                + "</body></html>"))

    def reads(self):
        return sum(method == "GET" and path in {
            BASE + "/_termdeck/api/v1/windows/one", BASE + "/_termdeck/api/v1/windows/two"
        } for method, path, _ in self.requests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=shutil.which("brave") or shutil.which("chromium"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / ".termdeck/qualification/tab-names")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    checks = {}
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=args.evidence_dir) as temporary:
        fixture = Fixture(Path(temporary))
        automatic, manual = fixture.ids
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser, headless=not args.headed,
                                                  args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(viewport={"width": 1400, "height": 900})
                context.add_cookies([{"name": "termdeck_csrf", "value": "fixture", "url": ORIGIN}])
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script(TRANSPORT_FIXTURE)
                page.route(ORIGIN + "/**", fixture.route)
                page.clock.install()

                def title(item):
                    return page.locator(f'.workspace-tab[data-pane-id="{item}"] .workspace-tab-title')

                def refresh():
                    expect(page.locator("#termdeck-app")).to_have_attribute("aria-busy", "false")
                    before = fixture.reads()
                    with page.expect_response(lambda response: response.url.endswith(("/windows/one", "/windows/two"))):
                        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(page.locator("#termdeck-app")).to_have_attribute("aria-busy", "false")
                    assert fixture.reads() == before + 1, (before, fixture.reads(), fixture.requests[-5:])

                def rename_dialog():
                    page.locator(".workspace-actions summary").click()
                    page.locator(".workspace-action-panel").get_by_role("button", name="Rename tab", exact=True).click()
                    return page.locator('#workspace-action-dialog input[name="rename"]')

                def rename(value):
                    rename_dialog().fill(value)
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/tab")) as mutation:
                        page.get_by_role("button", name="Save name", exact=True).click()
                    assert mutation.value.ok, mutation.value.text()
                    expect(page.locator("#workspace-action-dialog")).not_to_be_visible()

                page.goto(ORIGIN + BASE + "/one/t/" + automatic)
                expect(title(automatic)).to_have_text("arch")
                fixture.live[automatic] = "/cyber-a/bug-bounty/ipa"
                fixture.live[manual] = "/cyber-a/bug-bounty/ipa"
                fixture.generation += 1
                refresh()
                expect(title(automatic)).to_have_text("ipa")
                expect(title(manual)).to_have_text("outreach")
                expect(page.locator(f'.workspace-sidebar-tab[data-pane-id="{automatic}"] strong')).to_have_text("ipa")
                entry = rename_dialog()
                expect(entry).to_have_value("")
                expect(entry).to_have_attribute("placeholder", "ipa")
                page.get_by_role("button", name="Cancel", exact=True).click()
                checks["directory_change_all_surfaces_and_manual_isolation"] = True

                rename("arch")
                assert fixture.store.get(automatic).automatic_name is False
                fixture.live[automatic] = "/server/changed"
                fixture.generation += 1
                refresh()
                expect(title(automatic)).to_have_text("arch")
                expect(rename_dialog()).to_have_value("arch")
                page.get_by_role("button", name="Cancel", exact=True).click()
                rename("")
                expect(title(automatic)).to_have_text("changed")
                assert fixture.store.get(automatic).automatic_name is True
                checks["manual_basename_and_return_to_automatic"] = True

                old = copy.deepcopy(fixture.envelope("one"))
                fixture.live[automatic] = "/server/latest"
                fixture.generation += 1
                refresh()
                expect(title(automatic)).to_have_text("latest")
                fixture.override = old
                refresh()
                expect(title(automatic)).to_have_text("latest")
                fixture.override = fixture.envelope("one")
                fixture.override["window"]["panes"][0]["title"] = "equal timestamp"
                refresh()
                expect(title(automatic)).to_have_text("equal timestamp")
                del fixture.override["generated_at_unix_ms"]
                refresh()
                expect(title(automatic)).to_have_text("equal timestamp")
                assert page.locator("#termdeck-app").get_attribute("data-termdeck-state") == "error"
                fixture.override = None
                fixture.generation += 1
                refresh()
                expect(title(automatic)).to_have_text("latest")
                checks["older_equal_malformed_and_recovered_snapshots"] = True

                before = fixture.reads()
                page.clock.fast_forward(4500)
                page.wait_for_timeout(30)
                assert fixture.reads() == before, "idle polling accelerated unexpectedly"
                page.clock.fast_forward(600)
                page.wait_for_timeout(50)
                assert fixture.reads() == before + 1, "visible poll should run once after five seconds"
                fixture.hold = True
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(180)
                assert len(fixture.pending) == 1
                before = fixture.reads()
                for _ in range(3):
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    page.wait_for_timeout(130)
                assert fixture.reads() == before, "overlapping snapshot fetches are forbidden"
                fixture.hold = False
                pending, fixture.pending = fixture.pending, []
                for route, slug in pending:
                    fixture.respond_snapshot(route, slug)
                expect(page.locator("#termdeck-app")).to_have_attribute("aria-busy", "false")
                checks["bounded_five_second_poll_and_single_inflight_request"] = True

                # Offline/online and reload retain server-provided titles.
                page.evaluate("window.dispatchEvent(new Event('offline')); window.dispatchEvent(new Event('online'))")
                refresh()
                page.reload()
                expect(title(automatic)).to_have_text("latest")
                checks["reconnect_and_reload"] = True

                page.locator(".workspace-actions summary").click()
                page.get_by_role("button", name="Move tab to window…", exact=True).click()
                page.get_by_label("Destination window", exact=True).select_option("two")
                page.get_by_role("button", name="Move tabs", exact=True).click()
                page.wait_for_url(ORIGIN + BASE + "/two/t/" + automatic)
                expect(title(automatic)).to_have_text("latest")
                assert fixture.store.get(automatic).window_id == "two"
                assert fixture.store.get(automatic).automatic_name is True
                assert fixture.store.get(manual).window_id == "one"
                fixture.live[automatic] = "/server/<img onerror=alert(1)>"
                fixture.generation += 1
                refresh()
                expect(title(automatic)).to_have_text("<img onerror=alert(1)>")
                assert page.locator(".workspace-tabbar img, .workspace-sidebar img").count() == 0
                page.reload()
                expect(title(automatic)).to_have_text("<img onerror=alert(1)>")
                checks["cross_window_move_route_and_literal_unusual_title"] = True
                page.screenshot(path=str(args.evidence_dir / "automatic-tab-moved.png"))
                assert errors == [], errors
                result = {"ok": True, "checks": checks, "browser": browser.version,
                          "browser_executable": args.browser, "page_errors": errors,
                          "snapshot_requests": fixture.reads(),
                          "evidence_scope": "repository browser assets with disposable API/registry and inert terminal transport"}
                (args.evidence_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, sort_keys=True))
            finally:
                browser.close()
                fixture.store.close()


if __name__ == "__main__":
    main()
