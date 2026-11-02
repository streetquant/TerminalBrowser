#!/usr/bin/env python3
"""Compare actual tab-strip/sidebar render functions in disposable Chromium DOMs.

No server, credentials, WebSocket, real terminal, or production state is used.
The metric is synchronous JS + layout, not terminal or physical display latency.
A saved baseline directory must contain app.js and workspace-ui.js.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"

FACTORY = r"""
({app, ui}) => {
  function between(source, first, last) {
    const start = source.indexOf(first), end = source.indexOf(last, start);
    if (start < 0 || end <= start) throw new Error(`Missing production boundary: ${first}`);
    return source.slice(start, end);
  }
  const state = between(ui, '    let selectedId =', '    shell.dataset.sidebarSide = "right";');
  const update = between(ui, '    function updateTabs(', '    sidebarSearch.addEventListener("input"');
  const tabs = between(app, app.includes('    function tabButton(') ? '    function tabButton(' : '    function renderTabs(', '    function actionButton(');
  const code = `
    const element = (tag, className = "", text = "") => {
      const node = document.createElement(tag); node.className = className;
      if (text) node.textContent = text; return node;
    };
    const node = element;
    const button = (label, callback) => { const node = element("button", "", label); node.type = "button"; node.addEventListener("click", callback); return node; };
    const root = element("main", "workspace-app"); document.body.replaceChildren(root);
    const windowTitle = element("strong", "workspace-title"), workspaceStatus = element("span", "workspace-status");
    const tabbar = element("div", "workspace-tabbar"); tabbar.setAttribute("role", "tablist");
    const shell = element("section", "workspace-shell"); shell.dataset.sidebar = "open"; shell.dataset.sidebarSide = "right";
    const terminal = element("div", "xterm-shell", "Disposable UI fixture — no terminal transport"); shell.append(terminal);
    const sidebar = element("aside", "workspace-sidebar"), sidebarSearch = element("input", "workspace-sidebar-filter");
    const sidebarCount = element("p", "workspace-sidebar-count"), sidebarList = element("nav", "workspace-sidebar-list"), sidebarDetail = element("div", "workspace-sidebar-detail");
    sidebar.append(sidebarSearch, sidebarCount, sidebarList, sidebarDetail); shell.append(sidebar);
    const actionMenu = element("details", "workspace-actions"), actionSummary = element("summary", "", "Actions"); actionMenu.append(actionSummary);
    const menu = actionMenu; root.append(windowTitle, workspaceStatus, actionMenu, tabbar, shell);
    let envelope = null, activePaneId = "", lastTabSignature = "", pendingAction = "";
    const actions = [];
    const runAction = (action, pane) => actions.push({action, pane});
    const runTabAction = runAction;
    const selectPane = id => switchPane(id, true);
    function focusTabButton(id) { [...tabbar.querySelectorAll("[role=tab]")].find(node => node.dataset.paneId === id)?.focus(); }
    function switchPane(id) { activePaneId = id; renderTabs(false); }
    ${state}
    ${update}
    const workspaceTools = {updateTabs};
    const tabChrome = window.T1000TabChrome(tabbar, {selectPane, runAction, openMenu: () => {actionMenu.open = true; actionSummary.focus();}});
    ${tabs}
    return {
      root, tabbar, sidebarList, sidebarDetail, sidebarCount, actions,
      render(panes, id, pending = false) {
        envelope = {window: {name: "Benchmark window", panes}};
        activePaneId = id; pendingAction = pending ? "pending" : ""; renderTabs(false);
      },
      filter(query) { sidebarSearch.value = query; updateTabs(envelope.window.panes, activePaneId); },
      layout() { return root.getBoundingClientRect().height; }
    };
  `;
  window.makeTabChromeFixture = new Function(code);
}
"""

MEASURE = r"""
async ({count, samples}) => {
  const view = window.makeTabChromeFixture();
  const panes = Array.from({length: count}, (_, i) => ({id: String(i), title: `Project ${i}`, cwd: `/work/projects/project-${i}`, status: "running", pinned: i % 17 === 0, sessionId: `session-${i}`, controllerActive: false}));
  view.render(panes, "0"); view.layout();
  for (let i = 0; i < 8; i++) { view.render(panes, String(i % count)); view.layout(); }
  const before = [...view.tabbar.children];
  let mutations = 0;
  const observer = new MutationObserver(() => {});
  observer.observe(view.tabbar, {childList: true, subtree: true});
  observer.observe(view.sidebarList, {childList: true, subtree: true});
  const values = [];
  for (let i = 0; i < samples; i++) {
    await new Promise(requestAnimationFrame);
    const start = performance.now();
    view.render(panes, String((i + 8) % count)); view.layout();
    values.push(performance.now() - start);
    mutations += observer.takeRecords().filter(record => record.type === "childList").length;
  }
  observer.disconnect();
  const retained = before.filter(node => node.parentNode === view.tabbar).length;
  const sorted = [...values].sort((a,b) => a-b);
  return {count, samples, p50_ms: sorted[Math.floor(sorted.length * 0.5)], p95_ms: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))], max_ms: sorted.at(-1), raw_ms: values, child_list_mutations: mutations, retained_strip_rows: retained};
}
"""

VERIFY = r"""
() => {
  const view = window.makeTabChromeFixture(), checks = [];
  const ensure = (condition, name) => { if (!condition) throw new Error(name); checks.push(name); };
  let panes = Array.from({length: 140}, (_, i) => ({id: String(i), title: `Tab ${i}`, cwd: `/work/${i}`, status: "running", pinned: false, sessionId: `s-${i}`}));
  const selected = id => [...view.tabbar.querySelectorAll('[role=tab]')].find(node => node.dataset.paneId === id);
  view.render(panes, "1");
  const first = selected("1"), sidebar = view.sidebarList.children[1]; first.focus();
  view.render(panes, "2");
  ensure(selected("1") === first && view.sidebarList.children[1] === sidebar, "selection retains strip and sidebar nodes");
  ensure(document.activeElement === first, "selection preserves focused tab control");
  ensure(selected("2").getAttribute("aria-selected") === "true" && first.tabIndex === -1, "selection and roving tabindex update");
  const refreshed = panes.map(p => ({...p, sessionId: `new-${p.id}`}));
  view.render(refreshed, "2");
  first.dispatchEvent(new MouseEvent("dblclick", {bubbles:true, cancelable:true}));
  ensure(view.actions.at(-1).pane.sessionId === "new-1", "retained event handlers use latest session metadata");
  const row = first.parentNode;
  panes = refreshed.map(p => p.id === "1" ? {...p, title:"Changed", cwd:"/new", pinned:true, status:"paused"} : p);
  view.render(panes, "1", true);
  ensure(selected("1") === first && first.textContent.includes("Changed") && first.querySelector('.workspace-tab-pin'), "rename and pin update retained controls");
  ensure(row.querySelector('.workspace-tab-close').disabled, "pending action disables hide controls");
  view.render(panes, "1", false);
  ensure(!row.querySelector('.workspace-tab-close').disabled, "pending action release reenables controls");
  first.focus(); view.render([...panes].reverse(), "1");
  ensure(selected("1") === first && document.activeElement === first, "reorder retains node and keyboard focus");
  view.filter("/work/139");
  ensure(view.sidebarList.children.length === 1 && view.sidebarList.firstChild.dataset.paneId === "139", "filter locates pane outside first 100");
  view.filter("missing-text"); ensure(view.sidebarList.textContent === "No matching tabs", "empty filtered state is correct");
  view.filter(""); view.render(panes, "139");
  ensure(view.sidebarList.children.length === 100 && view.sidebarDetail.textContent.includes("Tab 139"), "bounded sidebar retains selected off-list detail");
  const detailRename = view.sidebarDetail.querySelector("button");
  view.render(panes.map(p => ({...p, sessionId:"latest"})), "139"); detailRename.click();
  ensure(view.actions.at(-1).pane.id === "139" && view.actions.at(-1).pane.sessionId === "latest", "sidebar detail actions use refreshed metadata");
  row.dispatchEvent(new MouseEvent("contextmenu", {bubbles:true, cancelable:true}));
  ensure(selected("1").getAttribute("aria-selected") === "true", "context menu selects the intended pane");
  view.render([], ""); ensure(!view.tabbar.children.length && view.sidebarList.textContent === "No matching tabs", "clearing tabs removes orphan controls");
  return checks;
}
"""


@contextmanager
def private_browser_environment():
    """Keep browser state in a short, unique workspace path and remove only it."""
    locations = (("TMPDIR", "t"), ("HOME", "h"), ("XDG_CACHE_HOME", "c"), ("XDG_CONFIG_HOME", "s"))
    previous = {key: os.environ.get(key) for key, _ in locations}
    # Chromium's Unix socket name must fit the platform limit; a nested report
    # directory is too long even when the browser profile itself is valid.
    with tempfile.TemporaryDirectory(prefix=".uc-", dir=ROOT) as temporary:
        try:
            for key, relative in locations:
                directory = Path(temporary) / relative
                directory.mkdir()
                os.environ[key] = str(directory)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", default=shutil.which("chromium") or "/usr/bin/chromium")
    parser.add_argument("--samples", type=int, default=60)
    args = parser.parse_args()
    if not 20 <= args.samples <= 200:
        parser.error("samples must be between 20 and 200")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output must stay inside this workspace")
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = {name: (ASSETS / name).read_text() for name in ("app.js", "workspace-ui.js")}
    baseline = {name: (args.baseline / name).read_text() for name in candidate}
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "metric": "production tab-strip + visible sidebar render JS and synchronous layout, milliseconds",
        "limitations": "Disposable headless Chromium with synthetic panes, no terminal transport, no physical-client or public-deployment latency claim.",
        "platform": platform.platform(), "load_average_before": os.getloadavg(),
        "hashes": {label: {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()} for label, files in (("baseline", baseline), ("candidate", candidate))},
        "runs": [],
    }
    with private_browser_environment(), sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser, headless=True)
        report["browser"] = browser.version
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(120000)
        page.set_content("<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>")
        page.add_style_tag(content=(ASSETS / "app.css").read_text())
        page.add_script_tag(content=candidate["app.js"])
        # Alternate order so candidate measurements do not always get the warmer run.
        for count in (1, 100, 1000):
            for label in (("baseline", "candidate") if count != 100 else ("candidate", "baseline")):
                files = baseline if label == "baseline" else candidate
                page.evaluate(FACTORY, {"app": files["app.js"], "ui": files["workspace-ui.js"]})
                result = page.evaluate(MEASURE, {"count": count, "samples": args.samples})
                result["version"] = label
                report["runs"].append(result)
        page.evaluate(FACTORY, {"app": candidate["app.js"], "ui": candidate["workspace-ui.js"]})
        report["checks"] = page.evaluate(VERIFY)
        context.close()
        browser.close()
    report["load_average_after"] = os.getloadavg()
    for count in (100, 1000):
        result = next(run for run in report["runs"] if run["count"] == count and run["version"] == "candidate")
        if result["child_list_mutations"] != 0 or result["retained_strip_rows"] != count:
            raise RuntimeError(f"Unexpected candidate DOM churn for {count} panes")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"browser": report["browser"], "checks": len(report["checks"]), "runs": [{k:v for k,v in run.items() if k != "raw_ms"} for run in report["runs"]], "report": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
