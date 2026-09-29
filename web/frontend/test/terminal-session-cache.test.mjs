import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../crates/termdeck-web-server/assets/terminal-session.js", import.meta.url), "utf8");

class Element {
  constructor() { this.children = []; this.dataset = {}; this.parent = null; }
  append(...children) { for (const child of children) this.insertBefore(child, null); }
  insertBefore(child, before) {
    child.remove();
    const index = before ? this.children.indexOf(before) : this.children.length;
    assert.ok(index >= 0);
    this.children.splice(index, 0, child); child.parent = this;
  }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
}

function fixture(limit = 4) {
  const window = {};
  vm.runInNewContext(source, {window, document: {createElement: () => new Element()}});
  const parent = new Element(), before = new Element(), shell = new Element();
  const host = new Element(), overlay = new Element(); shell.append(host, overlay); parent.append(shell, before);
  const mounts = new Map();
  const cache = window.T1000TerminalSession.createCache({parent, before, initial: {shell, host, overlay}, limit,
    create(view) {
      assert.equal(view.shell.parent, parent, "mount must measure an attached view");
      const mount = {activated: 0, deactivated: 0, disposed: 0,
        activate() { this.activated++; }, deactivate() { this.deactivated++; }, dispose() { this.disposed++; }};
      const previous = mounts.get(view.paneId) || []; previous.push(mount); mounts.set(view.paneId, previous);
      return mount;
    }});
  return {cache, parent, before, mounts};
}

test("returning to a recent tab preserves its renderer and attaches only that view", () => {
  const {cache, parent, mounts} = fixture();
  const first = cache.select("a"); first.host.selection = "retained history";
  const second = cache.select("b");
  assert.equal(first.mount.deactivated, 1); assert.equal(first.shell.parent, null);
  const returned = cache.select("a");
  assert.equal(returned, first); assert.equal(returned.host.selection, "retained history");
  assert.equal(first.mount.activated, 1); assert.equal(second.mount.deactivated, 1);
  assert.equal(second.shell.parent, null); assert.equal(parent.children.length, 2);
  assert.equal(mounts.get("a").length, 1); assert.equal(cache.select("a"), returned);
  assert.equal(first.mount.activated, 1, "already selected tab must not reconnect or refocus");
});

test("cache is bounded and evicts the least recently used stream", () => {
  const {cache, parent, mounts} = fixture();
  const a = cache.select("a"), b = cache.select("b"); cache.select("c"); cache.select("d"); cache.select("a");
  cache.select("e");
  assert.equal(parent.dataset.termdeckCachedTerminals, "4");
  assert.equal(b.mount.disposed, 1); assert.equal(a.mount.disposed, 0);
  const fresh = cache.select("b");
  assert.notEqual(fresh, b); assert.equal(mounts.get("b").length, 2);
  assert.equal(parent.children.length, 2);
});

test("removed or hidden tabs close their cached streams, including the selected tab", () => {
  const {cache, parent} = fixture(); const a = cache.select("a"), b = cache.select("b");
  cache.prune(["a"]);
  assert.equal(b.mount.disposed, 1); assert.equal(b.shell.parent, null);
  assert.equal(cache.select("a"), a); assert.equal(a.mount.activated, 1);
  cache.prune([]); cache.prune([]);
  assert.equal(a.mount.disposed, 1); assert.equal(b.mount.disposed, 1);
  assert.equal(parent.dataset.termdeckCachedTerminals, "0");
  const fresh = cache.select("a"); assert.notEqual(fresh, a);
});

test("navigation shutdown releases all renderers and streams exactly once", () => {
  const {cache, parent, before} = fixture(); const a = cache.select("a"), b = cache.select("b");
  cache.clear(); cache.clear();
  assert.equal(a.mount.disposed, 1); assert.equal(b.mount.disposed, 1);
  assert.deepEqual(parent.children, [before]); assert.equal(parent.dataset.termdeckCachedTerminals, "0");
});

test("invalid resource bounds are rejected", () => {
  for (const limit of [0, 5, -1, 1.5, "4", null]) assert.throws(() => fixture(limit), /cache bound/);
});
