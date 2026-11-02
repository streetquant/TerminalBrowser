import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const workspaceSource = readFileSync(
  new URL("../../crates/termdeck-web-server/assets/workspace-ui.js", import.meta.url), "utf8",
);

function environment() {
  let document;
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.className = "";
      this.textContent = "";
      this.dataset = {};
      this.attributes = {};
      this.children = [];
      this.parent = null;
      this.listeners = new Map();
      this.value = "";
      this.style = {};
    }
    get firstChild() { return this.children[0] || null; }
    get lastChild() { return this.children[this.children.length - 1] || null; }
    matches(selector) {
      if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
      return this.tagName === selector;
    }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return this.attributes[key]; }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    append(...nodes) {
      for (const node of nodes) { node.parent = this; this.children.push(node); }
    }
    contains(target) {
      return target === this || this.children.some(child => child.contains?.(target));
    }
    replaceChildren(...nodes) {
      if (this.contains(document.activeElement)) document.activeElement = null;
      for (const child of this.children) child.parent = null;
      this.children = [];
      this.append(...nodes);
    }
    querySelector(selector) {
      for (const child of this.children) {
        if (child.matches(selector)) return child;
        const result = child.querySelector(selector);
        if (result) return result;
      }
      return null;
    }
    closest(selector) {
      for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
      return null;
    }
    focus() { document.activeElement = this; }
  }
  document = { createElement: tag => new Element(tag), activeElement: null };
  const window = { T1000Management: { home() {} } };
  vm.runInNewContext(workspaceSource, { window, document, localStorage: { getItem: () => null } });
  // Rendering a window list does not require configuring management services.
  window.T1000Management.home = () => {};
  const root = new Element("main");
  const find = selector => root.querySelector(selector);
  const render = (snapshot, stale = false) => window.T1000UI.renderHome(root, snapshot, stale, path => path);
  return { root, document, render, find };
}

function snapshot(revision = 1) {
  return {
    revision, totalPanes: 2,
    windows: [{
      id: "window1", name: "Production", path: "/window1",
      panes: [{ title: "Shell", cwd: "/tmp" }, { title: "Editor", cwd: "/src" }],
    }],
  };
}

test("Windows list refresh keeps keyboard focus on Kill instead of Open", () => {
  const { render, find, document } = environment();
  render(snapshot());
  const oldButton = find(".window-list-kill");
  assert.ok(oldButton);
  oldButton.focus();
  render(snapshot(2));
  const newButton = find(".window-list-kill");
  assert.notEqual(newButton, oldButton);
  assert.equal(document.activeElement, newButton);
  assert.notEqual(document.activeElement, find(".window-list-row"));
});

test("Windows list refresh retains focus on the window-opening link", () => {
  const { render, find, document } = environment();
  render(snapshot());
  find(".window-list-row").focus();
  render(snapshot(2));
  assert.equal(document.activeElement, find(".window-list-row"));
});

test("Windows list refresh does not steal filter focus and updates stale status", () => {
  const { render, find, document } = environment();
  render(snapshot());
  const input = find(".window-filter");
  input.focus();
  render(snapshot(2), true);
  assert.equal(document.activeElement, input);
  assert.match(find(".window-list-status").textContent, /Connection interrupted/);
});

test("Windows list filtering a focused control does not focus another row", () => {
  const { render, find, document } = environment();
  render(snapshot());
  find(".window-list-kill").focus();
  find(".window-filter").value = "MissingWindow";
  render(snapshot(2));
  assert.equal(find(".window-list-kill"), null);
  assert.equal(document.activeElement, null);
});
