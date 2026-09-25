"use strict";

window.T1000Accounts = (() => {
  const node = (tag, text) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
  function mount(container, post) {
    let snapshot, busy = false;
    const retained = new Map();
    const notice = node("p"); notice.setAttribute("role", "status");
    const description = node("p", "Each terminal keeps its private Codex home. A switch uses the guarded restart lifecycle. Canonical login and account selection stay separate. Runtime credential memory cannot be inspected; restart observations are shown explicitly.");
    const controls = node("fieldset"); controls.className = "client-files-controls";
    const body = node("div"); body.className = "account-rows";
    const refresh = node("button", "Refresh account state"); refresh.type = "button";
    controls.append(refresh, body); container.append(description, notice, controls);
    async function action(fn) {
      if (busy) return;
      busy = true; controls.disabled = true; container.dataset.uploading = "true";
      try { await fn(); } catch (error) { notice.textContent = error.message; }
      finally { busy = false; controls.disabled = false; delete container.dataset.uploading; }
    }
    function select(accounts, label, current) {
      const picker = node("select"); picker.setAttribute("aria-label", label);
      for (const account of accounts) { const option = node("option", account.label); option.value = account.account_key; picker.append(option); }
      if (current && accounts.some(account => account.account_key === current)) picker.value = current;
      return picker;
    }
    async function change(rows, account, all) {
      const names = rows.map(row => `${row.name} (${row.id})`).join("\n");
      if (!confirm(`${all ? "Switch all tabs" : "Switch this tab"} to ${account}? Affected terminals may restart to reload credentials.\n\n${names}`)) return;
      const ids = rows.map(row => row.id);
      const request = {operation: all ? "switch-all-accounts" : "switch-account", ids, confirmation: ids, account, expected: Object.fromEntries(rows.map(row => [row.id, row.fingerprint]))};
      const signature = JSON.stringify(request);
      if (!retained.has(signature)) retained.set(signature, window.T1000Files.id());
      notice.textContent = "Switching selected terminals…";
      const result = await post({...request, request_id: retained.get(signature)});
      const failures = (result.results || []).filter(row => !row.ok);
      const message = failures.length ? failures.map(row => `${row.id}: ${row.error || "restart pending"}`).join(" · ") : "Selected terminal account transactions succeeded. Restart observations are shown below.";
      await load(); notice.textContent = message;
    }
    async function load() {
      snapshot = await post({operation: "accounts"}); body.replaceChildren();
      if (!snapshot.rows.length) { body.append(node("p", "No managed terminals have private Codex account integration enabled.")); return; }
      const healthy = snapshot.rows.filter(row => !row.error);
      if (healthy.length === snapshot.rows.length) {
        const shared = healthy[0].accounts.filter(account => healthy.every(row => row.accounts.some(candidate => candidate.account_key === account.account_key)));
        if (shared.length) {
          const global = node("section"); global.className = "account-global";
          global.append(node("h3", `All ${healthy.length} managed tabs (including stopped tabs)`));
          const picker = select(shared, "Account for all tabs");
          const button = node("button", "Switch all tabs…"); button.type = "button";
          button.addEventListener("click", () => action(() => change(healthy, picker.value, true)));
          global.append(picker, button); body.append(global);
        }
      }
      for (const row of snapshot.rows) {
        const section = node("section"); section.className = "client-file-row account-row"; section.dataset.sessionId = row.id;
        section.append(node("h3", `${row.window} / ${row.name}`));
        if (row.error) { section.append(node("p", row.error)); body.append(section); continue; }
        section.append(node("p", `Canonical account: ${row.canonical_account || "unselected/unknown"} (${row.canonical_credential_status})`));
        section.append(node("p", `Tab selection marker: ${row.marker_account_key || "unselected"} · effective credentials: ${row.effective_account || "unselected/unknown"}`));
        section.append(node("p", `Credential state: ${row.credential_status}${row.selection_conflict ? " · selection conflict" : ""}`));
        section.append(node("p", `Last observed process restart: ${row.running_account_key || "unknown"} · ${row.running_account_state} · restart needed: ${row.auth_restart_required || row.running_restart_required ? "yes / inspect" : "no"}`));
        if (row.pending_account) section.append(node("p", `Pending account: ${row.pending_account}`));
        if (row.accounts.length) {
          const picker = select(row.accounts, `Account for ${row.name}`, row.effective_account_key);
          const button = node("button", "Switch this tab…"); button.type = "button";
          button.addEventListener("click", () => action(() => change([row], picker.value, false)));
          section.append(picker, button);
        }
        body.append(section);
      }
    }
    refresh.addEventListener("click", () => action(load)); action(load);
  }
  return {mount};
})();
