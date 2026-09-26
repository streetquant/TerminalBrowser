"use strict";

window.T1000Files = (() => {
  let appPath, database;
  const id = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
  const key = () => `tdm1_${id()}${id()}`;
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  function field(label, control) { const node = el("label"); node.className = "workspace-field"; control.setAttribute("aria-label", label); node.append(el("span", label), control); return node; }
  function button(label, action) { const node = el("button", label); node.type = "button"; node.addEventListener("click", () => action()); return node; }
  function db() {
    if (!database) database = new Promise((resolve, reject) => {
      const request = indexedDB.open(`t1000-machine:${appPath("/")}`, 1);
      request.onupgradeneeded = () => request.result.createObjectStore("identity");
      request.onsuccess = () => {
        request.result.onversionchange = () => { request.result.close(); database = null; };
        resolve(request.result);
      };
      request.onerror = () => { database = null; reject(new Error("Private browser storage is unavailable. Machine enrollment needs browser storage.")); };
    });
    return database;
  }
  async function load() {
    const database = await db();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction("identity", "readonly");
      const request = transaction.objectStore("identity").get("current");
      let value;
      request.onsuccess = () => { value = request.result || null; };
      transaction.oncomplete = () => resolve(value);
      transaction.onerror = transaction.onabort = () => reject(new Error("Could not read machine enrollment"));
    });
  }
  const sameIdentity = (left, right) => JSON.stringify(left || null) === JSON.stringify(right || null);
  async function replaceIdentity(expected, value) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction("identity", "readwrite");
      const store = transaction.objectStore("identity");
      const request = store.get("current");
      let replacement = null;
      request.onsuccess = () => {
        // Read and compare inside the write transaction: another tab may have
        // reserved or completed a request since the caller read its identity.
        if (!sameIdentity(request.result, expected)) return;
        replacement = {...value, revision: id()};
        store.put(replacement, "current");
      };
      transaction.oncomplete = () => resolve(replacement);
      transaction.onerror = transaction.onabort = () => reject(new Error("Could not save machine enrollment; retry before closing this browser"));
    });
  }
  function csrf() {
    for (const part of document.cookie.split(";")) {
      const [name, ...rest] = part.trim().split("=");
      if (["termdeck_csrf", "__Host-termdeck_csrf"].includes(name)) return decodeURIComponent(rest.join("="));
    }
    throw new Error("Your login session expired. Sign in again.");
  }
  async function api(route, body, {machine = true, method = "POST", credential} = {}) {
    const headers = {Accept: "application/json"};
    if (method === "POST") Object.assign(headers, {"Content-Type": "application/json", "X-TermDeck-CSRF": csrf()});
    if (machine) {
      credential ??= (await finishPending())?.credential;
      if (!credential) throw new Error("Enroll this browser's machine before staging files.");
      headers["X-TermDeck-Machine-Key"] = credential;
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 180000);
    try {
      const response = await fetch(appPath(`/_termdeck/api/v1/${route}`), {method, headers, body: method === "POST" ? JSON.stringify(body) : undefined, credentials: "same-origin", cache: "no-store", signal: controller.signal});
      const result = await response.json();
      if (!response.ok || result.ok === false) {
        const error = new Error(response.status === 401 ? "Your login session expired. Sign in again." : result.error || `Request failed (${response.status})`);
        error.status = response.status; error.result = result; throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Request timed out. Retry the same action; its request identity is retained.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function checkedMachine(machine, expected) {
    if (!machine || !/^mach_[a-f0-9]{32}$/.test(machine.machine_id)
        || !/^acc_[a-f0-9]{64}$/.test(machine.account_id)
        || !Number.isSafeInteger(machine.credential_generation) || machine.credential_generation < 1
        || machine.revoked_at_ms != null) throw new Error("The host did not confirm an active machine. Stored credentials were preserved.");
    if (expected && (machine.machine_id !== expected.machine_id || machine.account_id !== expected.account_id)) {
      throw new Error("The host confirmed a different machine or account. Stored credentials were preserved.");
    }
    return machine;
  }
  async function identify(credential, expected) {
    return checkedMachine((await api("machines/current", null, {method: "GET", credential})).machine, expected);
  }
  const changedIdentity = () => new Error("Machine enrollment changed in another tab. Refresh and retry.");
  async function finishPending() {
    for (let attempt = 0; attempt < 8; attempt++) {
      const identity = await load();
      if (!identity?.pending) return identity;
      const pending = identity.pending;
      const credential = pending.new_credential || pending.credential;
      const expected = pending.operation === "rotate" ? {...identity.machine, machine_id: pending.machine_id} : null;
      let machine;
      try {
        // A committed response may have been lost, including across a new
        // login whose session cannot replay the original mutation receipt.
        try { machine = await identify(credential, expected); }
        catch (error) {
          if (error.status !== 403) throw error;
          machine = checkedMachine((await api("machines", pending, {machine: false})).machine, expected);
        }
      } catch (error) {
        if (!sameIdentity(await load(), identity)) continue;
        throw error;
      }
      const confirmed = await replaceIdentity(identity, {credential, machine});
      if (confirmed) return confirmed;
    }
    throw changedIdentity();
  }
  async function enroll(label) {
    for (let attempt = 0; attempt < 8; attempt++) {
      const identity = await load();
      if (identity?.pending) return finishPending();
      if (identity?.credential) {
        try { await identify(identity.credential, identity.machine); return identity; }
        catch (error) { if (error.status !== 403) throw error; }
      }
      const pending = {operation: "enroll", request_id: id(), credential: key(), label};
      if (await replaceIdentity(identity, {...identity, pending})) return finishPending();
    }
    throw changedIdentity();
  }
  async function rotate(machine) {
    for (let attempt = 0; attempt < 8; attempt++) {
      const identity = await load();
      if (identity?.pending) return finishPending();
      if (!identity?.credential || identity.machine?.machine_id !== machine.machine_id
          || identity.machine?.account_id !== machine.account_id) throw changedIdentity();
      const pending = {operation: "rotate", request_id: id(), machine_id: machine.machine_id, credential: identity.credential, new_credential: key()};
      if (await replaceIdentity(identity, {...identity, pending})) return finishPending();
    }
    throw changedIdentity();
  }
  async function current() {
    for (let attempt = 0; attempt < 8; attempt++) {
      const identity = await finishPending();
      if (!identity?.credential) return null;
      try { return await identify(identity.credential, identity.machine); }
      catch (error) { if (sameIdentity(await load(), identity)) throw error; }
    }
    throw changedIdentity();
  }
  async function management(body) { return api("management", body); }
  async function staging(operation, fields = {}) { return api("client-files", {operation, ...fields}); }
  function encode(bytes) { let value = ""; for (const byte of bytes) value += String.fromCharCode(byte); return btoa(value); }
  async function digest(file) { return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer())), b => b.toString(16).padStart(2, "0")).join(""); }
  function referenceKey(reference) { return `${reference.object_id}:${reference.generation}:${reference.digest}`; }
  function normalizedPaths(entries) {
    const paths = new Set(), components = new Map();
    return entries.map(entry => {
      const original = entry.path.split("/");
      const normalized = original.map(part => part.normalize("NFC"));
      const path = normalized.join("/");
      const collision = () => new Error("Selected file or directory names collide after Unicode normalization. Rename the conflicting items and select them again.");
      if (paths.has(path) || Array.from(paths).some(other => path.startsWith(`${other}/`) || other.startsWith(`${path}/`))) throw collision();
      paths.add(path);
      for (let depth = 1; depth <= normalized.length; depth++) {
        const prefix = normalized.slice(0, depth).join("/"), source = original.slice(0, depth).join("/");
        if (components.has(prefix) && components.get(prefix) !== source) throw collision();
        components.set(prefix, source);
      }
      return path;
    });
  }

  function mount(container, {selectable = false, onChange = () => {}} = {}) {
    container.classList.add("client-files");
    let machine = null, busy = false, selected = new Map(), objects = [], retry = null;
    const status = el("p", "Checking this browser's machine enrollment…"); status.setAttribute("role", "status"); status.className = "client-machine-status";
    const notice = el("p"); notice.setAttribute("role", "status"); notice.className = "client-files-notice";
    const identityControls = el("div"), uploadControls = el("div"), list = el("div");
    list.className = "client-files-list";
    const progress = el("progress"); progress.max = 1; progress.value = 0; progress.hidden = true; progress.setAttribute("aria-label", "File upload progress");
    const mapping = el("p", "Local client files → staged on the TermDeck host → attached to a remote task. Only files you explicitly select are read.");
    mapping.className = "subtle";
    const controls = el("fieldset"); controls.className = "client-files-controls";
    controls.append(identityControls, uploadControls, list);
    container.append(status, mapping, progress, notice, controls);
    const report = message => { notice.textContent = message; };
    async function action(operation) {
      if (busy) { report("Wait for the current upload or machine action."); return; }
      busy = true; controls.disabled = true; container.dataset.uploading = "true";
      try { await operation(); }
      catch (error) {
        if (error.status === 403) { try { await refresh(); } catch (_) { /* Keep the authorization error visible. */ } }
        report(error.uploadFailed ? `Upload failed: ${error.message} Use Retry upload while this selection remains open.` : error.message);
      }
      finally { busy = false; controls.disabled = false; delete container.dataset.uploading; }
    }
    function choose(object, checked) {
      const identity = referenceKey(object.reference);
      if (checked) selected.set(identity, object.reference); else selected.delete(identity);
      onChange();
    }
    async function inspect(object, row) {
      const data = await staging("inspect", {reference: object.reference});
      let detail = row.querySelector(".client-file-detail");
      if (!detail) { detail = el("div"); detail.className = "client-file-detail"; row.append(detail); }
      const metadata = el("div");
      metadata.append(el("p", `Original client reference: ${data.object.display_path}`), el("p", `Uploaded from ${data.object.machine_label} · ${data.object.status} · generation ${data.object.reference.generation}`));
      for (const file of data.object.files) metadata.append(el("p", `${file.path} · ${file.size} bytes · SHA-256 ${file.digest || "pending"}`));
      detail.replaceChildren(metadata);
      for (const attachment of data.object.attachments || []) {
        detail.append(button(`Detach from ${attachment.target}`, () => action(async () => {
          if (!confirm("Detach this immutable file copy from the remote task?")) return;
          await staging("detach", {request_id: id(), reference: object.reference, attachment_id: attachment.attachment_id});
          await inspect(object, row);
        })));
      }
    }
    function renderObjects() {
      list.replaceChildren();
      for (const object of objects) {
        const row = el("article"); row.className = "client-file-row"; row.dataset.objectId = object.reference.object_id; row.dataset.generation = String(object.reference.generation); row.dataset.status = object.status;
        const title = el("strong", object.label);
        const metadata = el("p", `${object.display_path} · ${machine?.label || object.machine_label} · ${object.status} · generation ${object.reference.generation} · ${object.reference.size} bytes · SHA-256 ${object.reference.digest || "pending"}`);
        row.append(title, metadata);
        if (selectable) {
          const select = el("input"); select.type = "checkbox"; select.disabled = object.status !== "ready" || !machine;
          select.checked = selected.has(referenceKey(object.reference));
          select.addEventListener("change", () => choose(object, select.checked));
          row.append(field(`Attach ${object.label} generation ${object.reference.generation}`, select));
        }
        const controls = el("div"); controls.className = "client-file-actions";
        controls.append(button("Details / attachments", () => action(() => inspect(object, row))));
        if (object.status === "ready") {
          for (const directory of [false, true]) {
            const picker = el("input"); picker.type = "file"; picker.multiple = true; picker.hidden = true;
            if (directory) picker.setAttribute("webkitdirectory", "");
            picker.addEventListener("change", () => action(() => upload(Array.from(picker.files || []).map(file => ({file, path: file.webkitRelativePath || file.name})), object.reference)));
            controls.append(button(directory ? "Refresh directory…" : "Refresh files…", () => picker.click()), picker);
          }
        }
        if (!["removed", "failed"].includes(object.status)) controls.append(button("Remove staged copy", () => action(async () => {
          if (!confirm("Remove this staged host copy? The original client file is unchanged. Attached copies must be detached first.")) return;
          await staging("remove", {request_id: id(), reference: object.reference}); await refreshObjects();
        })));
        row.append(controls); list.append(row);
      }
      if (!objects.length && machine) list.append(el("p", "No staged copies for this machine."));
    }
    async function refreshObjects() {
      if (!machine) { objects = []; selected.clear(); renderObjects(); onChange(); return; }
      objects = [];
      let offset = 0;
      do {
        const page = await staging("list", {offset}); objects.push(...page.objects); offset = page.next_offset;
      } while (offset !== null && objects.length < 4096);
      const ready = new Set(objects.filter(o => o.status === "ready").map(o => referenceKey(o.reference)));
      for (const value of selected.keys()) if (!ready.has(value)) selected.delete(value);
      renderObjects(); onChange();
    }
    async function upload(entries, previous = null) {
      if (!machine) throw new Error("Enroll this browser's machine before uploading.");
      if (!entries.length || entries.length > 256 || entries.some(e => e.file.size > 16 * 1024 * 1024) || entries.reduce((sum, e) => sum + e.file.size, 0) > 64 * 1024 * 1024) throw new Error("Select 1–256 files, at most 16 MiB each and 64 MiB total.");
      const paths = normalizedPaths(entries);
      progress.hidden = false; progress.value = 0;
      report(previous ? "Changed files selected. Preparing a new immutable generation…" : "Preparing selected client files…");
      const metadata = [];
      for (const [index, entry] of entries.entries()) metadata.push({path: paths[index], size: entry.file.size, modified_ms: entry.file.lastModified, media_type: entry.file.type || "application/octet-stream", digest: await digest(entry.file)});
      const body = {request_id: id(), files: metadata, label: entries.length === 1 ? entries[0].file.name : `${entries.length} selected files`, display_path: entries.length === 1 ? entries[0].path : entries.map(e => e.path).slice(0, 3).join(", ").slice(0, 1000), ...(previous ? {previous} : {})};
      const finalizeId = id();
      retry = async () => {
        try {
          const initiated = await staging("initiate", body);
          let object = initiated.object;
          if (object.status === "ready") { progress.value = 1; retry = null; choose(object, true); await refreshObjects(); report("Copy is already ready on the TermDeck host."); return; }
          if (!["uploading", "finalizing"].includes(object.status)) throw new Error(`Upload is ${object.status}; inspect or remove the incomplete copy.`);
          const total = entries.reduce((sum, e) => sum + e.file.size, 0);
          let uploaded = 0;
          for (let index = 0; index < entries.length; index++) {
            const file = entries[index].file;
            let offset = object.files[index].written;
            uploaded += offset;
            while (offset < file.size) {
              const bytes = new Uint8Array(await file.slice(offset, offset + 256 * 1024).arrayBuffer());
              report(`Uploading ${entries[index].path} · ${uploaded}/${total} bytes`);
              const response = await staging("chunk", {reference: object.reference, file_index: index, offset, data: encode(bytes)});
              uploaded += response.written - offset; offset = response.written;
              progress.value = total ? uploaded / total : 1;
            }
          }
          object = (await staging("finalize", {request_id: finalizeId, reference: object.reference})).object;
          if (object.status !== "ready") throw new Error(`Copy is ${object.status}; refresh before attaching.`);
          progress.value = 1; choose(object, true); retry = null;
          report(`Ready on TermDeck host · generation ${object.reference.generation} · ${object.reference.digest}. Select it to attach to the remote task.`);
          await refreshObjects();
        } catch (error) { error.uploadFailed = true; throw error; }
      };
      await retry();
    }
    function uploadInputs() {
      uploadControls.replaceChildren();
      if (!machine) return;
      for (const directory of [false, true]) {
        const input = el("input"); input.type = "file"; input.multiple = true;
        if (directory) input.setAttribute("webkitdirectory", "");
        input.addEventListener("change", () => action(() => upload(Array.from(input.files || []).map(file => ({file, path: file.webkitRelativePath || file.name})))));
        uploadControls.append(field(directory ? "Select local directory" : "Select local files", input));
      }
      uploadControls.append(button("Retry upload", () => action(async () => { if (!retry) throw new Error("No interrupted upload is available in this page."); await retry(); })),
        button("Refresh staged list", () => action(refreshObjects)),
        button("Clean expired partial uploads", () => action(async () => { await staging("cleanup", {request_id: id()}); await refreshObjects(); })));
      const drop = el("div", "Drop selected files or directories here"); drop.className = "client-file-drop";
      drop.addEventListener("dragover", event => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
      drop.addEventListener("drop", event => {
        event.preventDefault(); const items = Array.from(event.dataTransfer.items || []); const files = Array.from(event.dataTransfer.files || []);
        action(async () => {
          const selected = [];
          async function visit(entry, prefix = "", depth = 0) {
            if (depth > 16 || selected.length >= 256) throw new Error("Directory selection exceeds depth or file-count limits.");
            if (entry.isFile) { const file = await new Promise((resolve, reject) => entry.file(resolve, reject)); selected.push({file, path: prefix + file.name}); }
            else if (entry.isDirectory) {
              const reader = entry.createReader(); let children;
              do { children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject)); for (const child of children) await visit(child, `${prefix}${entry.name}/`, depth + 1); } while (children.length);
            }
          }
          const entries = items.map(item => item.webkitGetAsEntry?.()).filter(Boolean);
          if (entries.length) for (const entry of entries) await visit(entry);
          else selected.push(...files.map(file => ({file, path: file.name})));
          await upload(selected);
        });
      });
      uploadControls.append(drop);
    }
    async function renderIdentity() {
      identityControls.replaceChildren();
      const identity = await load();
      const label = el("input"); label.maxLength = 128; label.value = machine?.label || identity?.pending?.label || identity?.machine?.label || ""; label.placeholder = "e.g. Work laptop";
      identityControls.append(field("Machine label", label));
      if (identity?.pending) {
        identityControls.append(button("Retry machine action", () => action(async () => {
          await finishPending(); await refresh(); report("Pending machine action confirmed.");
        })));
        if (!machine) return;
      }
      if (!machine) {
        identityControls.append(button("Enroll this browser's machine", () => action(async () => {
          if (!label.value.trim()) throw new Error("Enter a machine label.");
          await enroll(label.value.trim()); await refresh();
        })));
        return;
      }
      identityControls.append(button("Rename machine", () => action(async () => {
        const identity = await finishPending();
        await api("machines", {operation: "rename", request_id: id(), machine_id: machine.machine_id, credential: identity.credential, label: label.value.trim()}, {machine: false}); await refresh();
      })), button("Rotate machine credential", () => action(async () => {
        await rotate(machine); await refresh(); report("Machine credential rotated. Existing login sessions remain separate.");
      })), button("List enrolled machines", () => action(async () => {
        const data = await api("machines", null, {method: "GET", machine: false});
        let devices = identityControls.querySelector(".enrolled-machines"); if (!devices) { devices = el("div"); devices.className = "enrolled-machines"; identityControls.append(devices); }
        devices.replaceChildren();
        for (const device of data.machines) {
          const row = el("p", `${device.label} · ${device.machine_id} · ${device.revoked_at_ms ? "revoked" : "enrolled"}`);
          if (!device.revoked_at_ms) row.append(button(`Revoke ${device.label}`, () => action(async () => {
            if (!confirm(`Revoke ${device.label}? This stops its future staging and attachment operations.`)) return;
            await api("machines", {operation: "revoke", request_id: id(), machine_id: device.machine_id}, {machine: false}); await refresh();
          })));
          devices.append(row);
        }
      })));
    }
    async function refresh() {
      try {
        machine = await current();
        status.textContent = machine ? `${machine.label} · ${machine.machine_id} · enrolled` : "This browser has no enrolled machine. A login cookie does not enroll it.";
      } catch (error) {
        machine = null; selected.clear();
        status.textContent = `Machine unavailable: ${error.message}`;
        if (error.status === 403) {
          const identity = await load();
          const list = await api("machines", null, {method: "GET", machine: false});
          const found = list.machines.find(row => row.machine_id === identity?.machine?.machine_id);
          if (found?.revoked_at_ms) status.textContent = `${found.label} · ${found.machine_id} · revoked. Staging and attachments are disabled.`;
        }
      }
      await renderIdentity(); uploadInputs(); await refreshObjects(); onChange();
    }
    refresh().catch(error => { status.textContent = error.message; });
    return {refresh, get busy() { return busy; }, get machine() { return machine; }, references: () => Array.from(selected.values())};
  }
  return {configure: api => { appPath = api.appPath; }, id, mount, management, current};
})();
