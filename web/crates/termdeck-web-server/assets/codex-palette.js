"use strict";

window.T1000CodexPalette = ({ open, element, button, body, modal, getPanes, getSelectedId, getGeneration, snapshot, appPath }) => {
    function commandPalette() {
      if (!open("Launch Codex in directories")) return;
      modal.onkeydown = null;
      const active = getPanes().find(pane => pane.id === getSelectedId());
      const parent = element("input");
      parent.value = active?.cwd || "/home/arch";
      parent.maxLength = 4096;
      parent.spellcheck = false;
      parent.autocomplete = "off";
      const directory = element("select");
      directory.setAttribute("aria-label", "Directory to launch Codex in");
      const prompt = element("textarea");
      prompt.maxLength = 8192;
      prompt.rows = 5;
      prompt.placeholder = "What should Codex do in the selected directories?";
      const status = element("p", "subtle");
      status.setAttribute("role", "status");
      const results = element("div", "workspace-picker-results");
      const refresh = button("Refresh directories", loadDirectories);
      const launch = button("Launch Codex", launchSelected);
      launch.className = "primary";
      launch.disabled = true;
      const actions = element("div", "workspace-dialog-actions");
      actions.append(refresh, launch);
      const field = (label, control) => { const row = element("label", "workspace-field"); row.append(element("span", "", label), control); return row; };
      body.append(element("p", "subtle", "The prompt runs on the TermDeck host in each selected immediate subdirectory. All starts one Codex window per directory."),
        field("Parent directory on TermDeck host", parent), field("Directory", directory), field("Codex prompt", prompt), actions, status, results);
      let options = null;
      let batch = null;
      let serial = 0;
      async function loadDirectories() {
        if (batch) return;
        const current = ++serial;
        refresh.disabled = launch.disabled = true;
        status.textContent = "Checking host directories…";
        directory.replaceChildren();
        try {
          const found = await window.T1000Files.management({operation: "launch-options", directory: parent.value.trim()});
          if (current !== serial || !modal.open) return;
          if (!found.runtimes.some(runtime => runtime.id === "codex")) throw new Error("Codex is unavailable in the TermDeck launch policy.");
          options = found;
          const all = element("option", "", `All immediate subdirectories (${found.directories.length})`);
          all.value = "all";
          directory.append(all);
          for (const [index, item] of found.directories.entries()) {
            const option = element("option", "", item.name);
            option.value = String(index);
            directory.append(option);
          }
          if (found.directories.length) directory.value = "0";
          status.textContent = found.directories.length ? `${found.directories.length} host directories available. Choose one or All.` : "No immediate subdirectories are available here.";
          launch.disabled = !found.directories.length;
        } catch (error) {
          if (current === serial && modal.open) { options = null; status.textContent = error.message; }
        } finally { if (current === serial && modal.open) refresh.disabled = false; }
      }
      async function launchSelected() {
        if (!options || launch.disabled || batch?.every(item => item.result)) return;
        const task = prompt.value.trim();
        if (!task) { status.textContent = "Enter a Codex prompt before launching."; prompt.focus(); return; }
        if (!batch) {
          const chosen = directory.value === "all" ? options.directories : [options.directories[Number(directory.value)]];
          if (!chosen.length || chosen.some(item => !item)) { status.textContent = "Choose an available directory."; return; }
          batch = chosen.map(item => ({...item, requestId: window.T1000Files.id(), result: null, error: ""}));
          parent.disabled = directory.disabled = prompt.disabled = refresh.disabled = true;
        }
        launch.disabled = true;
        results.replaceChildren(...batch.map(item => {
          const row = element("p", "");
          row.append(element("strong", "", `${item.name}: `));
          if (item.link) { const link = element("a", "", "Open window"); link.href = item.link; row.append(link); }
          else row.append(element("span", "", item.error || (item.result ? "Created; waiting for the live window snapshot" : "Waiting")));
          item.row = row;
          return row;
        }));
        const started = getGeneration();
        for (const item of batch) {
          if (started !== getGeneration() || !modal.open) break;
          if (item.result) continue;
          status.textContent = `Launching ${item.name}…`;
          try {
            item.result = await window.T1000Files.management({operation: "create-window", request_id: item.requestId,
              policy_revision: options.policy_revision, name: item.name, workspace_path: item.path,
              runtime: "codex", yolo: true, tabs: [item.name], task, attachments: []});
            item.error = "";
          } catch (error) { item.error = error.message; }
          if (started !== getGeneration() || !modal.open) break;
          item.row.lastChild.textContent = item.error || "Created; waiting for the live window snapshot";
        }
        const failed = batch.filter(item => !item.result);
        if (started !== getGeneration() || !modal.open) return;
        status.textContent = failed.length ? `${batch.length - failed.length} created, ${failed.length} failed. Retry uses the same request IDs.` : `${batch.length} Codex window${batch.length === 1 ? "" : "s"} created.`;
        launch.textContent = "Retry unresolved";
        launch.disabled = !failed.length;
        try {
          for (let attempt = 0; attempt < 12; attempt++) {
            const host = await snapshot();
            for (const item of batch) {
              if (!item.result || item.link) continue;
              const win = host.windows.find(win => win.id === item.result.window_id);
              if (!win) continue;
              item.link = appPath(win.path);
              item.row.lastChild?.remove();
              const link = element("a", "", "Open window"); link.href = item.link; item.row.append(link);
            }
            if (batch.every(item => !item.result || item.link)) break;
            await new Promise(resolve => setTimeout(resolve, 250));
          }
        } catch (error) { status.textContent += ` Snapshot refresh failed: ${error.message}`; }
        if (batch.length === 1 && batch[0].link && !failed.length) location.assign(batch[0].link);
      }
      parent.addEventListener("change", () => { if (!batch) loadDirectories(); });
      loadDirectories();
      prompt.focus();
    }
  return commandPalette;
};
