import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const assets = new URL("../../crates/termdeck-web-server/assets/", import.meta.url);
const appSource = readFileSync(new URL("app.js", assets), "utf8");
const workspaceSource = readFileSync(new URL("workspace-ui.js", assets), "utf8");

function fixture(sidebar = false) {
  const counts = { created: 0, inserted: 0, removed: 0, listeners: 0, attributes: 0, text: 0, focus: 0 };
  const document = { activeElement: null, getElementById: () => null };
  class Element {
    constructor(tag) {
      counts.created++;
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.parentNode = null;
      this.dataset = {};
      this.attributes = new Map();
      this.events = new Map();
      this.className = "";
      this.disabled = false;
      this._text = "";
      this.style = { values: new Map(), setProperty(name, value) { this.values.set(name, String(value)); } };
      this.classList = {
        toggle: (name, enabled) => {
          const names = new Set(this.className.split(/\s+/).filter(Boolean));
          if (enabled) names.add(name); else names.delete(name);
          this.className = [...names].join(" ");
        },
      };
    }
    get firstChild() { return this.children[0] || null; }
    get nextSibling() {
      if (!this.parentNode) return null;
      return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { counts.text++; this._text = String(value); for (const child of [...this.children]) child.remove(); }
    setAttribute(name, value) { counts.attributes++; this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    contains(item) { return Boolean(item) && (this === item || this.children.some(child => child.contains(item))); }
    insertBefore(item, before) {
      assert.ok(!before || before.parentNode === this, "insertBefore anchor belongs to container");
      if (item === before) return item;
      if (item.parentNode) item.remove();
      counts.inserted++;
      const index = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(index, 0, item);
      item.parentNode = this;
      return item;
    }
    append(...items) { for (const item of items) this.insertBefore(item, null); }
    prepend(item) { this.insertBefore(item, this.firstChild); }
    remove() {
      if (!this.parentNode) return;
      if (this.contains(document.activeElement)) document.activeElement = null;
      const parent = this.parentNode;
      parent.children.splice(parent.children.indexOf(this), 1);
      this.parentNode = null;
      counts.removed++;
    }
    replaceChildren(...items) { for (const child of [...this.children]) child.remove(); this._text = ""; this.append(...items); }
    addEventListener(name, handler) {
      counts.listeners++;
      const handlers = this.events.get(name) || [];
      handlers.push(handler);
      this.events.set(name, handlers);
    }
    dispatch(name, extra = {}) {
      const event = { pointerId: undefined, ...extra, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
      for (const handler of this.events.get(name) || []) handler(event);
      return event;
    }
    getBoundingClientRect() { return { width: 0, height: 0 }; }
    focus() { counts.focus++; document.activeElement = this; }
  }
  document.createElement = tag => new Element(tag);
  const window = { addEventListener() {} };
  vm.runInNewContext(appSource, { window, document });
  const container = document.createElement("nav");
  const selected = [], actions = [], menus = [];
  const options = { sidebar, selectPane: id => selected.push(id), runAction: (action, pane) => actions.push({ action, pane }), openMenu: () => menus.push(true) };
  const chrome = window.T1000TabChrome(container, options);
  return { document, window, container, chrome, counts, selected, actions, menus, options,
    reset: () => { for (const key of Object.keys(counts)) counts[key] = 0; } };
}
const pane = (id, extra = {}) => ({ id: String(id), title: `Tab ${id}`, cwd: `/work/${id}`, status: "running", pinned: false, sessionId: `session-${id}`, ...extra });
const panes = count => Array.from({ length: count }, (_, index) => pane(index));
const assertNoChurn = f => { for (const key of ["created", "inserted", "removed", "listeners", "text"]) assert.equal(f.counts[key], 0, key); };

for (const sidebar of [false, true]) {
  test(`${sidebar ? "sidebar" : "strip"}: selection reuses controls without traversing panes`, () => {
    const f = fixture(sidebar);
    let forbidScan = false;
    const data = new Proxy(panes(1000), { get(target, key, receiver) {
      if (forbidScan && [Symbol.iterator, "map", "filter", "find", "some"].includes(key)) throw new Error("selection scanned panes");
      return Reflect.get(target, key, receiver);
    } });
    f.chrome.render(data, "0");
    const before = [...f.container.children];
    f.reset(); forbidScan = true;
    for (let index = 1; index <= 100; index++) f.chrome.render(data, String(index));
    assertNoChurn(f);
    assert.deepEqual(f.container.children, before);
    assert.equal(f.chrome.get("100").getAttribute(sidebar ? "aria-current" : "aria-selected"), "true");
    assert.equal(f.chrome.get("0").getAttribute(sidebar ? "aria-current" : "aria-selected"), "false");
    assert.ok(f.counts.attributes <= (sidebar ? 200 : 400));
  });
  test(`${sidebar ? "sidebar" : "strip"}: unchanged metadata refresh keeps focus, nodes and listeners`, () => {
    const f = fixture(sidebar), data = panes(40);
    f.chrome.render(data, "4");
    const focused = f.chrome.get("4"); focused.focus();
    const before = [...f.container.children];
    f.reset();
    const refreshed = data.map(value => ({ ...value, sessionId: "fresh-session" }));
    f.chrome.render(refreshed, "4");
    assertNoChurn(f); assert.equal(f.counts.attributes, 0); assert.equal(f.counts.focus, 0);
    assert.deepEqual(f.container.children, before); assert.equal(f.document.activeElement, focused);
    focused.dispatch("dblclick");
    assert.equal(f.actions[0].pane, refreshed[4], "events use fresh nonvisual metadata");
  });
}

test("strip: rename, pin and pending-state updates retain correct actions", () => {
  const f = fixture(), data = panes(2);
  f.chrome.render(data, "0");
  const select = f.chrome.get("0"), row = select.parentNode, close = row.children[1];
  const updated = [pane(0, { title: "<untrusted>", pinned: true, cwd: "/new" }), data[1]];
  f.chrome.render(updated, "0", true);
  assert.equal(f.chrome.get("0"), select);
  assert.equal(select.children[0].className, "workspace-tab-pin");
  assert.equal(select.children[0].getAttribute("aria-hidden"), "true");
  assert.equal(select.children[1].textContent, "<untrusted>");
  assert.equal(row.title, "<untrusted>\n/new");
  assert.equal(close.getAttribute("aria-label"), "Hide <untrusted>");
  assert.ok(f.container.children.every(value => value.children[1].disabled));
  f.chrome.render(updated, "1", false);
  assert.ok(f.container.children.every(value => !value.children[1].disabled));
  const event = close.dispatch("click");
  assert.ok(event.prevented && event.stopped);
  assert.equal(f.actions[0].action, "hide"); assert.equal(f.actions[0].pane, updated[0]);
  f.chrome.render(data, "1");
  assert.equal(select.children.length, 1); assert.equal(select.title, "");
});

test("strip: roving keyboard focus, reorder, remove and empty transitions", () => {
  const f = fixture(), data = panes(3);
  f.container.append(f.document.createElement("span"));
  f.chrome.render(data, "");
  assert.equal(f.container.children.length, 3);
  assert.equal(f.chrome.get("0").getAttribute("tabindex"), "0");
  assert.equal(f.chrome.get("1").getAttribute("tabindex"), "-1");
  f.chrome.render(data, "1");
  assert.equal(f.chrome.get("0").getAttribute("tabindex"), "-1");
  const focused = f.chrome.get("1"); focused.focus();
  f.chrome.render([data[2], data[0], data[1]], "1");
  assert.deepEqual(f.container.children.map(row => row.dataset.paneId), ["2", "0", "1"]);
  assert.equal(f.chrome.get("1"), focused); assert.equal(f.document.activeElement, focused);
  f.chrome.render([data[0], data[2]], "0");
  assert.equal(f.chrome.get("1"), undefined); assert.notEqual(f.document.activeElement, focused);
  f.chrome.render([], ""); assert.equal(f.container.children.length, 0);
  f.chrome.render(data, "2"); assert.equal(f.container.children.length, 3);
  assert.equal(f.chrome.get("2").getAttribute("aria-selected"), "true");
});

test("sidebar: empty/filter transitions and current metadata are correct", () => {
  const f = fixture(true), data = panes(4);
  f.chrome.render([], "");
  assert.equal(f.container.textContent, "No matching tabs");
  f.chrome.render(data, "2"); const retained = f.chrome.get("2");
  f.chrome.render([pane(2, { pinned: true, status: "paused", cwd: "/changed" })], "2");
  assert.equal(f.chrome.get("2"), retained); assert.equal(f.container.children.length, 1);
  assert.equal(retained.children[0].textContent, "◆ Tab 2");
  assert.equal(retained.children[1].textContent, "/changed");
  assert.equal(retained.title, "Tab 2\n/changed\npaused");
  f.chrome.render([], ""); assert.equal(f.container.children.length, 1);
  assert.equal(f.chrome.get("2"), undefined);
});

test("context menus and opaque pane IDs keep selection and action targeting", () => {
  for (const sidebar of [false, true]) {
    const f = fixture(sidebar), id = 'a"] #unexpected', data = [pane(id), pane("other")];
    f.chrome.render(data, id);
    const select = f.chrome.get(id), row = sidebar ? select : select.parentNode;
    row.dispatch("contextmenu");
    assert.equal(f.selected.length, sidebar ? 1 : 0); assert.equal(f.menus.length, 1);
    select.dispatch("click"); assert.equal(f.selected.at(-1), id);
    select.dispatch("dblclick"); assert.equal(f.actions.at(-1).pane, data[0]);
  }
});

// Execute the production sidebar state and update function, not a copied model.
function sidebarFixture() {
  const f = fixture(true);
  const element = (tag, className = "", text = "") => {
    const item = f.document.createElement(tag); item.className = className;
    if (text) item.textContent = text; return item;
  };
  const context = {
    window: f.window, document: f.document, element,
    button: (label, callback) => { const item = element("button", "", label); item.addEventListener("click", callback); return item; },
    sidebar: { hidden: false }, sidebarSearch: { value: "" }, sidebarList: f.container,
    sidebarCount: element("p"), sidebarDetail: element("div"),
    selectPane: f.options.selectPane, runAction: f.options.runAction,
    menu: { querySelector: () => element("summary") },
  };
  const state = workspaceSource.slice(workspaceSource.indexOf('    let selectedId ='), workspaceSource.indexOf('    shell.dataset.sidebarSide = "right";'));
  const update = workspaceSource.slice(workspaceSource.indexOf('    function updateTabs('), workspaceSource.indexOf('    sidebarSearch.addEventListener("input"'));
  assert.ok(state.includes("sidebarChrome") && update.includes("sidebarChrome.render"));
  vm.runInNewContext(`${state}\n${update}\nglobalThis.update = updateTabs;`, context);
  return { ...f, ...context };
}

test("production sidebar: bounds, filtering, selected pane outside first 100 and no selection scans", () => {
  const f = sidebarFixture(); let forbid = false;
  const data = new Proxy(panes(1000), { get(target, key, receiver) {
    if (forbid && [Symbol.iterator, "map", "filter", "find"].includes(key)) throw new Error("sidebar rescanned on selection");
    return Reflect.get(target, key, receiver);
  } });
  f.update(data, "0");
  assert.equal(f.container.children.length, 100); assert.equal(f.sidebarCount.textContent, "100 of 1000 tabs");
  const first = f.container.firstChild;
  forbid = true; f.update(data, "999");
  assert.equal(f.container.firstChild, first); assert.match(f.sidebarDetail.textContent, /Tab 999/);
  forbid = false; f.sidebarSearch.value = "/work/999"; f.update(data, "999");
  assert.equal(f.container.children.length, 1); assert.equal(f.container.firstChild.dataset.paneId, "999");
  assert.equal(f.sidebarCount.textContent, "1 of 1000 tabs");
  f.sidebarSearch.value = "not present"; f.update(data, "999");
  assert.equal(f.container.textContent, "No matching tabs");
});

test("production sidebar: details actions see fresh invisible metadata and hidden changes catch up", () => {
  const f = sidebarFixture(), data = panes(3);
  f.update(data, "1");
  const rename = f.sidebarDetail.children[2].children[0];
  const refreshed = data.map(value => ({ ...value, sessionId: "new" }));
  f.update(refreshed, "1");
  assert.equal(f.sidebarDetail.children[2].children[0], rename);
  rename.dispatch("click"); assert.equal(f.actions.at(-1).pane, refreshed[1]);
  f.sidebar.hidden = true; f.update([pane(4)], "4");
  f.sidebar.hidden = false; f.update([pane(4)], "4");
  assert.equal(f.container.firstChild.dataset.paneId, "4");
  assert.match(f.sidebarDetail.textContent, /Tab 4/);
});

test("sidebar resize: drags both dock directions, clamps, saves settled widths and resets", () => {
  const f = fixture();
  f.window.innerWidth = 1200;
  f.window.matchMedia = () => ({ matches: true });
  const shell = f.document.createElement("section");
  const sidebar = f.document.createElement("aside");
  const saved = [];
  const resize = f.window.T1000SidebarResize(shell, sidebar, { onChange: width => saved.push(width) });
  sidebar.getBoundingClientRect = () => ({ width: resize.get(), height: 0 });
  const handle = sidebar.children[0];
  assert.equal(handle.className, "workspace-sidebar-resize");
  assert.equal(handle.getAttribute("aria-valuemin"), "220");
  assert.equal(handle.getAttribute("aria-valuemax"), "560");
  assert.equal(handle.getAttribute("aria-valuenow"), "280");
  assert.equal(shell.style.values.get("--td-sidebar-width"), "280px");

  handle.dispatch("pointerdown", { button: 0, clientX: 500 });
  handle.dispatch("pointermove", { clientX: 400 });
  assert.equal(handle.getAttribute("aria-valuenow"), "380");
  assert.equal(shell.dataset.resizing, "true");
  assert.deepEqual(saved, []);
  handle.dispatch("pointerup");
  assert.deepEqual(saved, [380]);
  assert.equal(shell.dataset.resizing, undefined);

  handle.dispatch("keydown", { key: "ArrowRight" });
  assert.equal(handle.getAttribute("aria-valuenow"), "364");
  shell.dataset.sidebarSide = "left";
  handle.dispatch("keydown", { key: "ArrowRight" });
  assert.equal(handle.getAttribute("aria-valuenow"), "380");
  handle.dispatch("keydown", { key: "Home" });
  assert.equal(handle.getAttribute("aria-valuenow"), "220");
  handle.dispatch("keydown", { key: "End" });
  assert.equal(handle.getAttribute("aria-valuenow"), "560");
  handle.dispatch("dblclick");
  assert.equal(handle.getAttribute("aria-valuenow"), "280");
  assert.deepEqual(saved, [380, 364, 380, 220, 560, 280]);

  shell.dataset.sidebarSide = "right";
  handle.dispatch("pointerdown", { button: 0, clientX: 500 });
  handle.dispatch("pointermove", { clientX: 0 });
  assert.equal(handle.getAttribute("aria-valuenow"), "560");
  handle.dispatch("pointerup");
  handle.dispatch("pointerdown", { button: 0, clientX: 500 });
  handle.dispatch("pointermove", { clientX: 1000 });
  assert.equal(handle.getAttribute("aria-valuenow"), "220");
  handle.dispatch("pointerup");
  assert.deepEqual(saved.slice(-2), [560, 220]);

  const restoredShell = f.document.createElement("section");
  const restoredSidebar = f.document.createElement("aside");
  f.window.T1000SidebarResize(restoredShell, restoredSidebar, { initial: 480 });
  const restoredHandle = restoredSidebar.children[0];
  assert.equal(restoredHandle.getAttribute("aria-valuenow"), "480");
  restoredHandle.dispatch("dblclick");
  assert.equal(restoredHandle.getAttribute("aria-valuenow"), "280");
});

test("modified assets remain below the per-asset size limit and use the shared renderer", () => {
  assert.ok(Buffer.byteLength(appSource) <= 65536);
  assert.ok(Buffer.byteLength(workspaceSource) <= 65536);
  assert.match(appSource, /tabChrome\.render\(envelope\.window\.panes/);
  assert.doesNotMatch(appSource, /lastTabSignature|panes\.map\(tabButton\)/);
});
