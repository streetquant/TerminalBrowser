import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../crates/termdeck-web-server/assets/client-files.js", import.meta.url), "utf8");
const turn = () => new Promise(resolve => setImmediate(resolve));
const clone = value => structuredClone(value);
const account = `acc_${"a".repeat(64)}`;
const machineId = number => `mach_${number.toString(16).padStart(32, "0")}`;
const secret = number => `tdm1_${number.toString(16).padStart(64, "0")}`;

class Element {
  constructor(tag) {
    this.tag = tag; this.textContent = ""; this.value = ""; this.children = [];
    this.attributes = {}; this.handlers = {}; this.dataset = {}; this.classList = {add() {}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelector(selector) { return this.all().find(node => (node.className || "").split(" ").includes(selector.slice(1))) || null; }
}

// A serialized transaction model, not a browser IndexedDB implementation.
// Writes become visible only on completion; aborted writes preserve the prior
// record. Independent VM contexts share the same origin/database transaction queue.
class Storage {
  databases = new Map();
  abortWrite = 0;
  writes = 0;
  entry(origin, name) {
    const key = `${origin}|${name}`;
    if (!this.databases.has(key)) this.databases.set(key, {record: null, queue: [], running: false});
    return this.databases.get(key);
  }
  identity(origin = "https://fixture.invalid", base = "") { return clone(this.entry(origin, `t1000-machine:${base}/`).record); }
  seed(record) { this.entry("https://fixture.invalid", "t1000-machine:/").record = clone(record); }
  forOrigin(origin) {
    return {open: name => {
      const request = {}, entry = this.entry(origin, name);
      setImmediate(() => {
        request.result = {close() {}, transaction: (_store, mode) => this.transaction(entry, mode)};
        request.onsuccess();
      });
      return request;
    }};
  }
  transaction(entry, mode) {
    const requests = [];
    const transaction = {objectStore: () => ({
      get: () => { const request = {}; requests.push({request, operation: "get"}); return request; },
      put: value => { const request = {}; requests.push({request, operation: "put", value: clone(value)}); return request; },
    })};
    entry.queue.push(() => {
      let snapshot = clone(entry.record), dirty = false;
      const finish = () => { entry.running = false; this.pump(entry); };
      const advance = () => setImmediate(() => {
        const next = requests.shift();
        if (next) {
          if (next.operation === "get") next.request.result = clone(snapshot);
          else { assert.equal(mode, "readwrite"); snapshot = clone(next.value); dirty = true; }
          next.request.onsuccess?.();
          advance();
        } else {
          if (dirty && ++this.writes === this.abortWrite) transaction.onabort?.();
          else {
            if (dirty) entry.record = snapshot;
            transaction.oncomplete?.();
          }
          finish();
        }
      });
      advance();
    });
    this.pump(entry);
    return transaction;
  }
  pump(entry) {
    if (entry.running || !entry.queue.length) return;
    entry.running = true;
    setImmediate(entry.queue.shift());
  }
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function fixture({enrolled = true} = {}) {
  const storage = new Storage(), records = new Map(), receipts = new Map(), requests = [];
  let nextMachine = 1, session = "login-a", authenticated = true, mutationFault = null, hold = null, proofOverride = null;
  const metadata = record => ({account_id: account, machine_id: record.machine_id, label: record.label,
    credential_generation: record.generation, revoked_at_ms: record.revoked ? 1 : null});
  if (enrolled) {
    const record = {machine_id: machineId(nextMachine++), label: "Fixture machine", credential: secret(1), generation: 1};
    records.set(record.machine_id, record);
    storage.seed({credential: record.credential, machine: metadata(record)});
  }
  const response = (status, data) => ({status, ok: status === 200, json: async () => clone(data)});
  const failure = (status, error = "Machine credential is not authorized") => response(status, {ok: false, error});
  const byCredential = credential => Array.from(records.values()).find(record => record.credential === credential && !record.revoked);
  async function fetch(path, options) {
    const body = options.body && JSON.parse(options.body);
    requests.push({path, method: options.method, body: clone(body), credential: options.headers["X-TermDeck-Machine-Key"], session});
    if (!authenticated) return failure(401, "Login expired");
    if (path.endsWith("/machines/current")) {
      const record = byCredential(options.headers["X-TermDeck-Machine-Key"]);
      if (!record) return failure(403);
      return response(200, {machine: proofOverride ? proofOverride(metadata(record)) : metadata(record)});
    }
    if (path.endsWith("/machines") && options.method === "GET") return response(200, {machines: Array.from(records.values(), metadata)});
    if (path.endsWith("/machines")) {
      const fault = mutationFault; mutationFault = null;
      if (fault === "before-commit") throw new TypeError("Fixture request interrupted before commit");
      const replayKey = `${session}:${body.request_id}`;
      const replay = receipts.get(replayKey);
      let record;
      if (replay) {
        if (replay.payload !== JSON.stringify(body)) return failure(409, "Request conflict");
        record = records.get(replay.machine_id);
        if (!record || record.revoked || record.credential !== (body.new_credential || body.credential)) return failure(403);
      } else if (body.operation === "enroll") {
        if (Array.from(records.values()).some(record => record.credential === body.credential)) return failure(409, "Credential already enrolled");
        record = {machine_id: machineId(nextMachine++), credential: body.credential, label: body.label, generation: 1};
        records.set(record.machine_id, record);
      } else {
        record = records.get(body.machine_id);
        if (!record || record.revoked || record.credential !== body.credential) return failure(403);
        if (body.operation === "rotate") { record.credential = body.new_credential; record.generation++; }
        else if (body.operation === "rename") record.label = body.label;
        else throw new Error("Unexpected synthetic machine operation");
      }
      receipts.set(replayKey, {payload: JSON.stringify(body), machine_id: record.machine_id});
      const result = response(200, {machine: metadata(record)});
      if (fault === "after-commit") throw new TypeError("Fixture response lost after commit");
      if (hold) { const active = hold; hold = null; active.entered.resolve(); await active.release.promise; }
      return result;
    }
    if (path.endsWith("/client-files")) {
      if (!byCredential(options.headers["X-TermDeck-Machine-Key"])) return failure(403);
      if (body.operation === "list") return response(200, {objects: [], next_offset: null});
      if (body.operation === "initiate") return response(200, {object: {status: "ready", files: body.files,
        reference: {object_id: `obj_${"c".repeat(32)}`, generation: 1, digest: "d".repeat(64)}}});
    }
    throw new Error(`Unexpected synthetic route: ${path}`);
  }
  async function tab({origin = "https://fixture.invalid", base = ""} = {}) {
    const window = {};
    vm.runInNewContext(source, {window, indexedDB: storage.forOrigin(origin), crypto: webcrypto, fetch,
      document: {cookie: "termdeck_csrf=fixture", createElement: tag => new Element(tag)},
      AbortController, setTimeout, clearTimeout, Uint8Array, confirm: () => true});
    window.T1000Files.configure({appPath: path => `${base}${path}`});
    const root = new Element("div"), view = window.T1000Files.mount(root);
    const button = text => root.all().find(node => node.tag === "button" && node.textContent === text);
    for (let tries = 0; tries < 200; tries++) {
      if (button("Rotate machine credential") || button("Enroll this browser's machine") || button("Retry machine action")) break;
      await turn();
    }
    assert.ok(button("Rotate machine credential") || button("Enroll this browser's machine") || button("Retry machine action"), "Initial identity controls appeared");
    for (let count = 0; count < 5; count++) await turn();
    return {root, view, files: window.T1000Files,
      field: label => root.all().find(node => node.attributes["aria-label"] === label),
      notice: () => root.querySelector(".client-files-notice").textContent,
      async click(text) { assert.ok(button(text), `Button exists: ${text}`); await button(text).handlers.click(); },
      async select(files, directory = false) {
        const input = root.all().find(node => node.attributes["aria-label"] === (directory ? "Select local directory" : "Select local files"));
        assert.ok(input); input.files = files; await input.handlers.change();
      },
    };
  }
  return {storage, tab, requests, records,
    machineRequests: () => requests.filter(request => request.path.endsWith("/machines") && request.method === "POST"),
    uploads: () => requests.filter(request => request.body?.operation === "initiate"),
    failMutation: when => { mutationFault = when; },
    renewLogin: () => { authenticated = true; session = "login-b"; },
    expireLogin: () => { authenticated = false; },
    changeProof: transform => { proofOverride = transform; },
    holdMutation() { hold = {entered: deferred(), release: deferred()}; return hold; },
  };
}

function assertConfirmed(f) {
  const identity = f.storage.identity();
  assert.ok(identity.credential); assert.equal(identity.pending, undefined);
  const record = f.records.get(identity.machine.machine_id);
  assert.equal(identity.credential, record.credential);
  assert.equal(identity.machine.credential_generation, record.generation);
  return record;
}

test("fresh origin and base-path stores do not inherit a logged-in machine", async () => {
  const f = fixture();
  const original = await f.tab();
  assert.equal(original.view.machine.machine_id, machineId(1));
  for (const scope of [{base: "/t1000"}, {origin: "https://another.invalid"}]) {
    const other = await f.tab(scope);
    assert.equal(await other.files.current(), null);
    assert.ok(other.field("Machine label"));
  }
  assert.equal(f.machineRequests().length, 0);
});

for (const operation of ["enroll", "rotate"]) {
  for (const newLogin of [false, true]) {
    test(`${operation}: lost committed response recovers the same identity${newLogin ? " after login renewal" : " on button retry"}`, async () => {
      const f = fixture({enrolled: operation === "rotate"}), page = await f.tab();
      if (operation === "enroll") page.field("Machine label").value = "Fixture enrollment";
      const button = operation === "enroll" ? "Enroll this browser's machine" : "Rotate machine credential";
      f.failMutation("after-commit"); await page.click(button);
      assert.match(page.notice(), /response lost/);
      const pending = f.storage.identity().pending;
      assert.equal(pending.operation, operation);
      assert.match(pending.new_credential || pending.credential, /^tdm1_[a-f0-9]{64}$/);
      const originalRequest = clone(f.machineRequests()[0].body);
      if (newLogin) f.renewLogin();
      await page.click(button);
      const record = assertConfirmed(f);
      assert.equal(record.machine_id, machineId(1));
      assert.equal(record.generation, operation === "rotate" ? 2 : 1);
      assert.equal(f.records.size, 1); assert.equal(f.machineRequests().length, 1);
      assert.deepEqual(originalRequest, pending);
    });
  }
  test(`${operation}: an uncommitted request retries the identical saved body`, async () => {
    const f = fixture({enrolled: operation === "rotate"}), page = await f.tab();
    if (operation === "enroll") page.field("Machine label").value = "Original label";
    const button = operation === "enroll" ? "Enroll this browser's machine" : "Rotate machine credential";
    f.failMutation("before-commit"); await page.click(button);
    const pending = clone(f.storage.identity().pending);
    page.field("Machine label").value = "Changed label must not replace pending request";
    await page.click(button);
    assertConfirmed(f);
    assert.equal(f.machineRequests().length, 2);
    assert.deepEqual(f.machineRequests().map(request => request.body), [pending, pending]);
  });
}

test("reload after a new login recovers a committed pending rotation from durable storage", async () => {
  const f = fixture(), before = await f.tab();
  f.failMutation("after-commit"); await before.click("Rotate machine credential");
  f.renewLogin();
  const after = await f.tab();
  assert.equal(after.view.machine.machine_id, machineId(1));
  assert.equal(assertConfirmed(f).generation, 2);
  assert.equal(f.machineRequests().length, 1);
});

test("a stale tab uses the latest durable credential for its next rotation", async () => {
  const f = fixture(), a = await f.tab(), b = await f.tab();
  await a.click("Rotate machine credential");
  const first = f.storage.identity().credential;
  await b.click("Rotate machine credential");
  assert.equal(assertConfirmed(f).generation, 3);
  assert.equal(f.machineRequests()[1].body.credential, first);
});

test("simultaneous tabs converge on one pending rotation", async () => {
  const f = fixture(), a = await f.tab(), b = await f.tab();
  await Promise.all([a.click("Rotate machine credential"), b.click("Rotate machine credential")]);
  assert.equal(assertConfirmed(f).generation, 2);
  assert.equal(new Set(f.machineRequests().map(request => JSON.stringify(request.body))).size, 1);
});

test("simultaneous fresh tabs retain one explicit enrollment and its original request", async () => {
  const f = fixture({enrolled: false}), a = await f.tab(), b = await f.tab();
  a.field("Machine label").value = "Label A"; b.field("Machine label").value = "Label B";
  await Promise.all([a.click("Enroll this browser's machine"), b.click("Enroll this browser's machine")]);
  assert.equal(f.records.size, 1);
  assert.equal(assertConfirmed(f).generation, 1);
  assert.equal(new Set(f.machineRequests().map(request => JSON.stringify(request.body))).size, 1);
});

test("a delayed completion cannot overwrite a newer tab's completed rotation", async () => {
  const f = fixture(), a = await f.tab();
  const hold = f.holdMutation(), pending = a.click("Rotate machine credential");
  await hold.entered.promise;
  const b = await f.tab();
  assert.equal(assertConfirmed(f).generation, 2);
  await b.click("Rotate machine credential");
  const latest = f.storage.identity();
  hold.release.resolve(); await pending;
  assert.deepEqual(f.storage.identity(), latest);
  assert.equal(assertConfirmed(f).generation, 3);
});

for (const changed of ["machine_id", "account_id"]) {
  test(`pending rotation rejects current-machine proof for a different ${changed}`, async () => {
    const f = fixture(), page = await f.tab();
    f.failMutation("after-commit"); await page.click("Rotate machine credential");
    const pending = f.storage.identity();
    f.changeProof(machine => ({...machine, [changed]: changed === "machine_id" ? machineId(9) : `acc_${"f".repeat(64)}`}));
    await page.click("Rotate machine credential");
    assert.match(page.notice(), /different machine or account/);
    assert.deepEqual(f.storage.identity(), pending);
    assert.equal(f.machineRequests().length, 1);
  });
}

test("expired login preserves the pending credential until authentication succeeds", async () => {
  const f = fixture(), page = await f.tab();
  f.failMutation("after-commit"); await page.click("Rotate machine credential");
  const pending = f.storage.identity();
  f.expireLogin(); await page.click("Rotate machine credential");
  assert.match(page.notice(), /Sign in again/);
  assert.deepEqual(f.storage.identity(), pending);
  f.renewLogin(); await page.click("Rotate machine credential");
  assert.equal(assertConfirmed(f).generation, 2);
});

test("revocation cannot be recovered as a confirmed pending credential", async () => {
  const f = fixture(), page = await f.tab();
  f.failMutation("after-commit"); await page.click("Rotate machine credential");
  const pending = f.storage.identity();
  f.records.get(machineId(1)).revoked = true;
  await page.click("Rotate machine credential");
  assert.deepEqual(f.storage.identity(), pending);
  assert.equal(page.view.machine, null);
  assert.equal(f.records.size, 1);
});

test("a revoked confirmed machine is replaced only by explicit enrollment", async () => {
  const f = fixture(), page = await f.tab(), previous = f.storage.identity();
  f.records.get(machineId(1)).revoked = true;
  await page.view.refresh();
  assert.equal(page.view.machine, null);
  assert.deepEqual(f.storage.identity(), previous);
  assert.equal(f.machineRequests().length, 0);
  page.field("Machine label").value = "Explicit replacement";
  await page.click("Enroll this browser's machine");
  assert.equal(assertConfirmed(f).machine_id, machineId(2));
  assert.equal(f.records.get(machineId(1)).revoked, true);
});

for (const write of [1, 2]) {
  test(`IndexedDB abort during ${write === 1 ? "reservation" : "confirmation"} preserves a recoverable identity`, async () => {
    const f = fixture(), page = await f.tab();
    f.storage.abortWrite = write;
    await page.click("Rotate machine credential");
    assert.match(page.notice(), /Could not save machine enrollment/);
    if (write === 1) {
      assert.equal(f.machineRequests().length, 0);
      assert.equal(assertConfirmed(f).generation, 1);
    } else {
      assert.ok(f.storage.identity().pending);
      assert.equal(f.machineRequests().length, 1);
    }
    await page.click("Rotate machine credential");
    assert.equal(assertConfirmed(f).generation, 2);
    assert.equal(f.machineRequests().length, 1);
  });
}

function file(name, relative = "") {
  const bytes = new TextEncoder().encode("selected fixture bytes");
  return {name, webkitRelativePath: relative, size: bytes.length, lastModified: 1, type: "text/plain",
    arrayBuffer: async () => bytes.slice().buffer};
}

test("selected filenames are NFC in the manifest while display metadata retains the original", async () => {
  const f = fixture(), page = await f.tab(), name = "cafe\u0301.txt";
  await page.select([file(name)]);
  assert.equal(f.uploads().length, 1);
  assert.equal(f.uploads()[0].body.files[0].path, "caf\u00e9.txt");
  assert.equal(f.uploads()[0].body.display_path, name);
  assert.equal(f.uploads()[0].body.label, name);
});

test("directory selection normalizes every component without merging original spellings", async () => {
  const f = fixture(), page = await f.tab();
  await page.select([file("re\u0301sume\u0301.txt", "cafe\u0301/re\u0301sume\u0301.txt"), file("other.txt", "cafe\u0301/other.txt")], true);
  assert.deepEqual(f.uploads()[0].body.files.map(entry => entry.path), ["caf\u00e9/r\u00e9sum\u00e9.txt", "caf\u00e9/other.txt"]);
});

for (const paths of [["cafe\u0301.txt", "caf\u00e9.txt"], ["cafe\u0301/a.txt", "caf\u00e9/b.txt"], ["caf\u00e9", "cafe\u0301/a.txt"]]) {
  test(`normalization rejects conflicting selections ${JSON.stringify(paths)}`, async () => {
    const f = fixture(), page = await f.tab();
    await page.select(paths.map(path => file(path.split("/").at(-1), path)), true);
    assert.match(page.notice(), /names collide after Unicode normalization/);
    assert.equal(f.uploads().length, 0);
  });
}
