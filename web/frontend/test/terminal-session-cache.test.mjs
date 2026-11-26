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

function fixture(limit = 16) {
  const window = {};
  vm.runInNewContext(source, {window, document: {createElement: () => new Element()}});
  const parent = new Element(), before = new Element(), shell = new Element();
  const host = new Element(), overlay = new Element(); shell.append(host, overlay); parent.append(shell, before);
  const mounts = new Map();
  const cache = window.T1000TerminalSession.createCache({parent, before, initial: {shell, host, overlay}, limit,
    create(view, options) {
      assert.equal(view.shell.parent, parent, "mount must measure an attached view");
      assert.equal(view.shell.dataset.termdeckActive, options?.initialActive === false ? "false" : "true");
      const mount = {activated: 0, deactivated: 0, disposed: 0,
        activate() { this.activated++; }, deactivate() { this.deactivated++; }, dispose() { this.disposed++; }};
      const previous = mounts.get(view.paneId) || []; previous.push(mount); mounts.set(view.paneId, previous);
      return mount;
    }});
  return {cache, parent, before, mounts};
}

test("returning to a recent tab preserves its renderer and keeps cached views mounted", () => {
  const {cache, parent, mounts} = fixture();
  const first = cache.select("a"); first.host.selection = "retained history";
  const second = cache.select("b");
  assert.equal(first.mount.deactivated, 1);
  assert.equal(first.shell.parent, parent);
  assert.equal(first.shell.dataset.termdeckActive, "false");
  assert.equal(first.shell.id, "");
  assert.equal(first.host.id, "");
  assert.equal(second.shell.dataset.termdeckActive, "true");
  assert.equal(second.shell.id, "terminal-shell");
  assert.equal(second.host.id, "terminal-xterm");
  const returned = cache.select("a");
  assert.equal(returned, first); assert.equal(returned.host.selection, "retained history");
  assert.equal(first.mount.activated, 1); assert.equal(second.mount.deactivated, 1);
  assert.equal(first.shell.dataset.termdeckActive, "true");
  assert.equal(first.shell.id, "terminal-shell");
  assert.equal(second.shell.parent, parent);
  assert.equal(second.shell.dataset.termdeckActive, "false");
  assert.equal(second.shell.id, "");
  assert.equal(parent.children.length, 3);
  assert.equal(mounts.get("a").length, 1); assert.equal(cache.select("a"), returned);
  assert.equal(first.mount.activated, 1, "already selected tab must not reconnect or refocus");
});

test("cache is bounded and evicts the least recently used stream", () => {
  const {cache, parent, mounts} = fixture(4);
  const a = cache.select("a"), b = cache.select("b"); cache.select("c"); cache.select("d"); cache.select("a");
  cache.select("e");
  assert.equal(parent.dataset.termdeckCachedTerminals, "4");
  assert.equal(b.mount.disposed, 1); assert.equal(b.shell.parent, null);
  assert.equal(a.mount.disposed, 0); assert.equal(a.shell.parent, parent);
  const fresh = cache.select("b");
  assert.notEqual(fresh, b); assert.equal(mounts.get("b").length, 2);
  assert.equal(parent.children.length, 5);
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
  for (const limit of [0, 17, -1, 1.5, "4", null]) assert.throws(() => fixture(limit), /cache bound/);
});

test("speculative warm mounts passive views, respects LRU priority, and activates cleanly", () => {
  const {cache, parent, mounts} = fixture(3);
  const a = cache.select("a");
  const w1 = cache.warm("w1");
  assert.ok(w1);
  assert.equal(w1.shell.parent, parent);
  assert.equal(w1.shell.dataset.termdeckActive, "false");
  assert.equal(w1.shell.id || "", "");
  assert.equal(a.shell.dataset.termdeckActive, "true");
  assert.equal(a.shell.id, "terminal-shell");
  assert.equal(cache.has("w1"), true);
  assert.equal(cache.warm("w1"), w1);

  const b = cache.select("b");
  assert.equal(cache.warm("c"), null, "idle warm does not evict when full");
  assert.equal(cache.has("c"), false);
  assert.equal(b.mount.disposed, 0);

  // Hover warm with evict=true evicts idle-warmed w1 before user-visited a or b.
  const c = cache.warm("c", {evict: true});
  assert.ok(c);
  assert.equal(w1.mount.disposed, 1);
  assert.equal(a.mount.disposed, 0);
  assert.equal(b.mount.disposed, 0);

  // Selecting pre-warmed c activates existing mount without recreating.
  const activatedC = cache.select("c");
  assert.equal(activatedC, c);
  assert.equal(c.mount.activated, 1);
  assert.equal(mounts.get("c").length, 1);
  assert.equal(c.shell.dataset.termdeckActive, "true");
  assert.equal(c.shell.id, "terminal-shell");
});
