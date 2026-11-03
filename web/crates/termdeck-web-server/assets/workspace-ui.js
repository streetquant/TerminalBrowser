"use strict";

window.T1000UI = (() => {
  function element(tag, className, text) {
    const item = document.createElement(tag);
    item.className = className || "";
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function button(text, handler) {
    const item = element("button", "", text);
    item.type = "button";
    item.addEventListener("click", handler);
    return item;
  }
  function preferences() {
    try {
      const value = JSON.parse(localStorage.getItem("t1000.appearance") || "{}");
      const choices = window.TermDeckTerminal?.appearanceOptions();
      return {
        fontSize: Number.isInteger(value?.fontSize) && value.fontSize >= 10 && value.fontSize <= 72 ? value.fontSize : 13,
        cursorStyle: ["block", "bar", "underline"].includes(value?.cursorStyle) ? value.cursorStyle : "block",
        cursorBlink: false,
        palette: choices?.palettes.some(item => item.value === value?.palette) ? value.palette : "t1000",
        font: choices?.fonts.some(item => item.value === value?.font) ? value.font : "auto",
        fontFamily: typeof value?.fontFamily === "string" && value.fontFamily.length <= 128 ? value.fontFamily : "",
        cursorColor: "",
        scrollback: Number.isInteger(value?.scrollback) && value.scrollback >= 100 && value.scrollback <= 500000 ? value.scrollback : 20000,
        chrome: ["noir", "dark", "light", "system"].includes(value?.chrome) ? value.chrome : "noir",
        density: value?.density === "comfortable" ? "comfortable" : "compact",
        uiFont: typeof value?.uiFont === "string" && value.uiFont.length <= 128 ? value.uiFont : "",
        uiScale: Number.isFinite(value?.uiScale) && value.uiScale >= .75 && value.uiScale <= 1.75 ? value.uiScale : 1,
        reduceMotion: value?.reduceMotion === true,
      };
    } catch (_) {
      return { fontSize: 13, cursorStyle: "block", cursorBlink: false, palette: "t1000", font: "auto", fontFamily: "", cursorColor: "", scrollback: 20000, chrome: "noir", density: "compact", uiFont: "", uiScale: 1, reduceMotion: false };
    }
  }
  function applyChrome(settings = preferences()) {
    document.documentElement.dataset.chrome = settings.chrome;
    document.documentElement.dataset.density = settings.density;
    document.documentElement.dataset.reduceMotion = String(settings.reduceMotion);
    document.documentElement.style.setProperty("--td-ui-scale", String(settings.uiScale));
    document.documentElement.style.setProperty("--td-ui-font", settings.uiFont || 'Cantarell, "Noto Sans", system-ui, sans-serif');
  }
  function attachWorkspace({ root, toolbar, menu, getPanes, selectPane, getTerminal, newTab, runAction, loadHidden, restoreHidden, getWindow, refresh, snapshot, appPath }) {
    const management = window.T1000Management.workspace({root, toolbar, menu, getWindow, refresh, getTerminal});
    const modal = element("dialog", "workspace-dialog workspace-tools-dialog");
    modal.id = "workspace-tools-dialog";
    const content = element("div", "workspace-tools-content");
    const heading = element("h2", "");
    heading.id = "workspace-tools-title";
    modal.setAttribute("aria-labelledby", heading.id);
    const close = button("Close", () => modal.close());
    const header = element("div", "workspace-tools-heading");
    header.append(heading, close);
    const body = element("div", "workspace-tools-body");
    content.append(header, body);
    modal.append(content);
    root.append(modal);
    window.T1000Backdrop(modal);
    let opener = null;
    let returnToTerminal = false;
    let generation = 0;
    const shell = root.querySelector(".workspace-shell");
    const sidebar = element("aside", "workspace-sidebar");
    sidebar.setAttribute("aria-label", "Tabs pane");
    sidebar.hidden = true;
    const sidebarHeader = element("div", "workspace-sidebar-heading");
    const sidebarTitle = element("h2", "", "Tabs");
    const sidebarMove = button("⇄", () => {
      shell.dataset.sidebarSide = shell.dataset.sidebarSide === "left" ? "right" : "left";
      saveLayout();
    });
    const sidebarClose = button("×", () => toggleSidebar(false));
    sidebarClose.setAttribute("aria-label", "Hide Tabs pane");
    sidebarHeader.append(sidebarTitle, sidebarMove, sidebarClose);
    const sidebarSearch = element("input", "workspace-sidebar-filter");
    sidebarSearch.type = "search";
    sidebarSearch.placeholder = "Search tabs";
    sidebarSearch.setAttribute("aria-label", "Filter Tabs pane");
    const sidebarCount = element("p", "workspace-sidebar-count");
    sidebarCount.setAttribute("role", "status");
    const sidebarList = element("nav", "workspace-sidebar-list");
    sidebarList.setAttribute("aria-label", "Window tabs");
    const sidebarDetail = element("div", "workspace-sidebar-detail");
    sidebar.append(sidebarHeader, sidebarSearch, sidebarCount, sidebarList, sidebarDetail);
    shell.append(sidebar);
    let selectedId = "", sidebarPanes = null, sidebarQuery = null;
    let sidebarMatches = [], sidebarLookup = new Map(), sidebarDetailSignature = "";
    const sidebarChrome = window.T1000TabChrome(sidebarList, {
      sidebar: true, selectPane, runAction,
      openMenu: () => { menu.open = true; menu.querySelector("summary").focus(); },
    });
    shell.dataset.sidebarSide = "right";
    let sidebarWidth = 280;
    try {
      const saved = JSON.parse(localStorage.getItem("t1000.layout") || "{}");
      if (saved?.side === "left") shell.dataset.sidebarSide = "left";
      sidebar.hidden = saved?.tabsPane !== true;
      if (Number.isFinite(saved?.width)) sidebarWidth = saved.width;
    } catch (_) { /* Storage is optional; use the desktop defaults. */ }
    shell.dataset.sidebar = sidebar.hidden ? "closed" : "open";
    window.T1000SidebarResize(shell, sidebar, {
      initial: sidebarWidth,
      onChange: width => { sidebarWidth = width; saveLayout(); },
    });
    function saveLayout() {
      sidebarMove.setAttribute("aria-label", `Move Tabs pane to ${shell.dataset.sidebarSide === "left" ? "right" : "left"}`);
      sidebarMove.title = sidebarMove.getAttribute("aria-label");
      try { localStorage.setItem("t1000.layout", JSON.stringify({ tabsPane: !sidebar.hidden, side: shell.dataset.sidebarSide, width: Math.round(sidebarWidth) })); }
      catch (_) { /* The pane still works without persistent browser storage. */ }
    }
    function toggleSidebar(show = sidebar.hidden) {
      menu.open = false;
      sidebar.hidden = !show;
      shell.dataset.sidebar = show ? "open" : "closed";
      saveLayout();
      updateTabs(getPanes(), selectedId);
      if (show) sidebarSearch.focus();
      else getTerminal()?.focus();
    }
    function updateTabs(panes, activeId) {
      selectedId = activeId;
      if (sidebar.hidden) return;
      const query = sidebarSearch.value.trim().toLocaleLowerCase();
      const changed = panes !== sidebarPanes;
      if (changed) sidebarLookup = new Map(panes.map(pane => [pane.id, pane]));
      if (changed || query !== sidebarQuery) {
        sidebarMatches = [];
        for (const pane of panes) {
          if (!query || `${pane.title} ${pane.cwd}`.toLocaleLowerCase().includes(query)) sidebarMatches.push(pane);
          if (sidebarMatches.length === 100) break;
        }
        sidebarPanes = panes;
        sidebarQuery = query;
        const count = `${sidebarMatches.length} of ${panes.length} tabs`;
        if (sidebarCount.textContent !== count) sidebarCount.textContent = count;
      }
      sidebarChrome.render(sidebarMatches, activeId);
      const active = sidebarLookup.get(activeId);
      const signature = JSON.stringify(active ? [active.id, active.title, active.status, active.cwd, active.pinned] : null);
      if (signature === sidebarDetailSignature) return;
      sidebarDetailSignature = signature;
      sidebarDetail.replaceChildren();
      if (active) {
        sidebarDetail.append(element("strong", "", active.title), element("span", "", `${active.status} · ${active.cwd}`));
        const actions = element("div", "workspace-sidebar-actions");
        for (const [label, action] of [["Rename", "rename"], [active.pinned ? "Unpin" : "Pin", active.pinned ? "unpin" : "pin"], ["Hide", "hide"]]) {
          actions.append(button(label, () => {
            const latest = sidebarLookup.get(selectedId);
            if (latest) runAction(action, latest);
          }));
        }
        sidebarDetail.append(actions);
      }
    }
    sidebarSearch.addEventListener("input", () => updateTabs(getPanes(), selectedId));
    sidebarSearch.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); toggleSidebar(false); }
    });
    saveLayout();
    modal.addEventListener("close", () => {
      if (root.querySelector("dialog[open]")) return;
      getTerminal()?.clearSearch();
      if (returnToTerminal) getTerminal()?.focus();
      else if (opener?.isConnected) opener.focus();
      else getTerminal()?.focus();
    });
    modal.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        modal.close();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = [...modal.querySelectorAll("input, button, select")].filter(control => !control.disabled && control.getClientRects().length);
      if (document.activeElement === (event.shiftKey ? controls[0] : controls.at(-1))) {
        event.preventDefault();
        (event.shiftKey ? controls.at(-1) : controls[0]).focus();
      }
    });
    function open(title) {
      if (root.querySelector("dialog[open]")) return false;
      opener = menu.open ? menu.querySelector("summary") : document.activeElement;
      returnToTerminal = false;
      generation += 1;
      menu.open = false;
      heading.textContent = title;
      body.replaceChildren();
      modal.showModal();
      return true;
    }
    function searchTabs() {
      if (!open("Search tabs")) return;
      const input = element("input", "");
      input.type = "search";
      input.placeholder = "Tab name or working directory";
      input.setAttribute("aria-label", input.placeholder);
      const results = element("div", "workspace-picker-results");
      const status = element("p", "subtle");
      status.setAttribute("role", "status");
      body.append(input, status, results);
      function render() {
        const query = input.value.trim().toLocaleLowerCase();
        const rows = [];
        for (const pane of getPanes()) {
          if (!`${pane.title} ${pane.cwd}`.toLocaleLowerCase().includes(query)) continue;
          const row = button("", () => { returnToTerminal = true; modal.close(); selectPane(pane.id); });
          row.className = "workspace-picker-row";
          row.append(element("strong", "", `${pane.pinned ? "◆ " : ""}${pane.title}`), element("span", "", pane.cwd));
          rows.push(row);
          if (rows.length >= 100) break;
        }
        results.replaceChildren(...rows);
        status.textContent = rows.length ? `${rows.length}${rows.length === 100 ? "+" : ""} matches` : "No matches";
      }
      input.addEventListener("input", render);
      modal.onkeydown = event => {
        const matches = [...results.querySelectorAll("button")];
        if (["ArrowDown", "ArrowUp"].includes(event.key) && matches.length) {
          event.preventDefault();
          const index = matches.indexOf(document.activeElement);
          const next = index < 0
            ? (event.key === "ArrowDown" ? 0 : matches.length - 1)
            : (index + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
          matches[next].focus();
        } else if (event.key === "Enter" && document.activeElement === input) {
          event.preventDefault();
          matches[0]?.click();
        }
      };
      render();
      input.focus();
    }
    const commandPalette = window.T1000CodexPalette({
      open, element, button, body, modal, getPanes,
      getSelectedId: () => selectedId, getGeneration: () => generation,
      snapshot, appPath,
    });
    function find() {
      if (!open("Find in terminal")) return;
      modal.onkeydown = null;
      const input = element("input", "");
      input.type = "search";
      input.setAttribute("aria-label", "Find text in terminal");
      const status = element("p", "subtle");
      status.setAttribute("role", "status");
      const search = reverse => {
        const terminal = getTerminal();
        const matched = input.value && (reverse ? terminal?.searchPrevious(input.value) : terminal?.searchNext(input.value));
        status.textContent = !input.value ? "Search the current tab’s scrollback." : matched ? "Match selected in terminal" : "No matches";
        if (!input.value) terminal?.clearSearch();
      };
      input.addEventListener("input", () => search(false));
      input.addEventListener("keydown", event => {
        if (event.key === "Enter") { event.preventDefault(); search(event.shiftKey); }
      });
      const actions = element("div", "workspace-dialog-actions");
      actions.append(button("Previous", () => search(true)), button("Next", () => search(false)));
      body.append(input, status, actions);
      search(false);
      input.focus();
    }
    async function hiddenTabs() {
      if (!open("Hidden tabs")) return;
      modal.onkeydown = null;
      const requestGeneration = generation;
      const status = element("p", "subtle", "Loading hidden tabs…");
      status.setAttribute("role", "status");
      body.append(status);
      close.focus();
      try {
        const tabs = await loadHidden();
        if (!modal.open || requestGeneration !== generation) return;
        if (!Array.isArray(tabs) || tabs.length > 16_384) throw new Error("Invalid hidden tab list.");
        status.textContent = tabs.length ? "Reopen a hidden tab without restarting its process." : "No hidden tabs in this window.";
        const filter = element("input", "");
        filter.type = "search";
        filter.setAttribute("aria-label", "Search hidden tabs");
        filter.placeholder = "Search hidden tabs";
        const list = element("div", "workspace-picker-results");
        function renderHidden() {
          const query = filter.value.trim().toLocaleLowerCase();
          const matches = tabs.filter(tab => `${tab.name} ${tab.cwd}`.toLocaleLowerCase().includes(query));
          list.replaceChildren();
          status.textContent = query || matches.length > 100
            ? `Showing ${Math.min(100, matches.length)} of ${matches.length} matching hidden tabs`
            : tabs.length ? "Reopen a hidden tab without restarting its process." : "No hidden tabs in this window.";
          for (const tab of matches.slice(0, 100)) {
            const row = button("", async () => {
              for (const control of list.querySelectorAll("button")) control.disabled = true;
              filter.disabled = true;
              status.textContent = `Reopening “${tab.name}”…`;
              try {
                await restoreHidden(tab.id);
                if (requestGeneration === generation && modal.open) {
                  returnToTerminal = true;
                  modal.close();
                }
              } catch (error) {
                status.textContent = `Could not reopen the tab. ${error.message}`;
                for (const control of list.querySelectorAll("button")) control.disabled = false;
                filter.disabled = false;
              }
            });
            row.className = "workspace-picker-row";
            row.append(element("strong", "", tab.name), element("span", "", tab.cwd));
            list.append(row);
          }
        }
        filter.addEventListener("input", renderHidden);
        body.append(filter, list);
        renderHidden();
      } catch (error) {
        if (modal.open && requestGeneration === generation) status.textContent = `Could not load hidden tabs. ${error.message}`;
      }
    }
    function appearance() {
      if (!open("Settings")) return;
      modal.onkeydown = null;
      const settings = preferences();
      const choices = window.TermDeckTerminal.appearanceOptions();
      const select = (items, value) => {
        const control = element("select", "");
        for (const item of items) {
          const option = element("option", "", item.label);
          option.value = item.value;
          control.append(option);
        }
        control.value = value;
        return control;
      };
      const palette = select(choices.palettes, settings.palette);
      const font = select(choices.fonts, settings.font);
      const size = element("input", "");
      size.type = "number";
      size.min = "10";
      size.max = "72";
      size.value = settings.fontSize;
      const cursor = element("select", "");
      for (const style of ["block", "bar", "underline"]) {
        const option = element("option", "", style);
        option.value = style;
        cursor.append(option);
      }
      cursor.value = settings.cursorStyle;
      const number = (value, min, max, step = 1) => { const input = element("input"); input.type = "number"; input.value = value; input.min = min; input.max = max; input.step = step; return input; };
      const text = value => { const input = element("input"); input.value = value || ""; input.maxLength = 128; return input; };
      const chrome = select(["noir", "dark", "light", "system"].map(value => ({value, label: {noir: "Graphite · amber", dark: "Slate · amber", light: "Warm paper · ochre", system: "Follow system"}[value]})), settings.chrome);
      const density = select(["compact", "comfortable"].map(value => ({value, label: value})), settings.density);
      const uiFont = text(settings.uiFont), customFont = text(settings.fontFamily);
      uiFont.placeholder = "System interface font"; customFont.placeholder = "Optional installed font family";
      const scale = number(settings.uiScale, .75, 1.75, .05), scrollback = number(settings.scrollback, 100, 500000, 100);
      const color = text("#ffffff"); color.placeholder = "Always white"; color.disabled = true;
      const motion = element("input"); motion.type = "checkbox"; motion.checked = settings.reduceMotion;
      const side = select(["left", "right"].map(value => ({value, label: value})), shell.dataset.sidebarSide);
      const paneVisible = element("input"); paneVisible.type = "checkbox"; paneVisible.checked = !sidebar.hidden;
      body.append(element("h3", "settings-section", "Terminal"));
      for (const [text, control] of [["Terminal palette", palette], ["Terminal font", font], ["Terminal text size (px)", size], ["Cursor shape", cursor]]) {
        control.setAttribute("aria-label", text);
        const label = element("label", "workspace-field");
        label.append(element("span", "", text), control);
        body.append(label);
      }
      for (const [text, control] of [["Custom terminal font", customFont], ["Cursor color", color], ["Scrollback lines", scrollback]]) {
        control.setAttribute("aria-label", text); const label = element("label", "workspace-field"); label.append(element("span", "", text), control); body.append(label);
      }
      body.append(element("h3", "settings-section", "Interface"));
      for (const [text, control] of [["Interface theme", chrome], ["Density", density], ["Interface font", uiFont], ["Interface scale", scale], ["Sidebar position", side], ["Show Tabs pane", paneVisible], ["Reduce motion", motion]]) {
        control.setAttribute("aria-label", text); const label = element("label", control.type === "checkbox" ? "workspace-check" : "workspace-field"); label.append(element("span", "", text), control); body.append(label);
      }
      const selectionSummary = () => `${chrome.selectedOptions[0]?.textContent || chrome.value} theme, ${font.selectedOptions[0]?.textContent || font.value} terminal font, ${uiFont.value.trim() || "system"} interface font`;
      const status = element("p", "subtle", `Current here: ${selectionSummary()}. Changes apply immediately and are saved in this browser. Fonts must be installed on this computer.`);
      status.setAttribute("role", "status");
      body.append(status);
      function apply() {
        if (![size, scale, scrollback, color].every(control => control.reportValidity()) || !Number.isInteger(Number(size.value))) return;
        const next = { fontSize: Number(size.value), cursorStyle: cursor.value, cursorBlink: false, palette: palette.value, font: font.value,
          fontFamily: customFont.value.trim(), cursorColor: "", scrollback: Number(scrollback.value), chrome: chrome.value, density: density.value, uiFont: uiFont.value.trim(), uiScale: Number(scale.value), reduceMotion: motion.checked };
        getTerminal()?.setAppearance(next);
        applyChrome(next);
        shell.dataset.sidebarSide = side.value; sidebar.hidden = !paneVisible.checked; shell.dataset.sidebar = sidebar.hidden ? "closed" : "open"; saveLayout(); updateTabs(getPanes(), selectedId);
        try {
          localStorage.setItem("t1000.appearance", JSON.stringify(next));
          status.textContent = `Applied here: ${selectionSummary()}. Saved in this browser.`;
        }
        catch (_) { status.textContent = "Applied for this tab. Browser storage is unavailable, so this cannot be saved."; }
        return next;
      }
      for (const control of [size, cursor, palette, font, customFont, color, scrollback, chrome, density, uiFont, scale, motion, side, paneVisible]) control.addEventListener("change", apply);
      body.append(button("Reset to defaults", () => {
        size.value = "13"; cursor.value = "block";
        palette.value = "t1000"; font.value = "auto"; customFont.value = ""; color.value = ""; scrollback.value = "20000";
        chrome.value = "noir"; density.value = "compact"; uiFont.value = ""; scale.value = "1"; motion.checked = false; side.value = "right"; paneVisible.checked = false; apply();
      }));
      body.append(element("h3", "settings-section", "Desktop defaults"));
      const syncStatus = element("p", "subtle", "Browser preferences stay local unless you explicitly save desktop defaults. Existing desktop terminals are not restarted.");
      syncStatus.setAttribute("role", "status");
      const consent = element("input"); consent.type = "checkbox";
      const consentLabel = element("label", "workspace-check"); consentLabel.append(consent, element("span", "", "Update desktop defaults for the next desktop launch"));
      let revision = null;
      const loadDesktop = button("Load desktop settings", async () => {
        loadDesktop.disabled = true;
        try {
          const data = await window.T1000Management.appearance({mode: "read"}); revision = data.revision;
          const value = data.values;
          size.value = value.fontSize; cursor.value = value.cursorStyle;
          palette.value = value.palette; font.value = value.font; customFont.value = value.fontFamily;
          color.value = ""; scrollback.value = value.scrollback; chrome.value = value.chrome;
          density.value = value.density; uiFont.value = value.uiFont; scale.value = value.uiScale;
          motion.checked = value.reduceMotion; side.value = value.side; paneVisible.checked = value.tabsPane;
          apply(); syncStatus.textContent = "Desktop settings loaded into this browser. Fonts still need to be installed on this computer.";
        } catch (error) { syncStatus.textContent = error.message; }
        finally { loadDesktop.disabled = false; }
      });
      const saveDesktop = button("Save desktop defaults", async () => {
        if (!consent.checked) return;
        const value = apply(); if (!value) return;
        saveDesktop.disabled = true;
        try {
          if (revision === null) revision = (await window.T1000Management.appearance({mode: "read"})).revision;
          const data = await window.T1000Management.appearance({mode: "save", revision, confirmation: "save-desktop-defaults", values: {...value, side: side.value, tabsPane: paneVisible.checked}});
          revision = data.revision; consent.checked = false;
          syncStatus.textContent = "Desktop defaults saved. They apply at the next desktop launch; current sessions were not changed.";
        } catch (error) { syncStatus.textContent = error.message; }
        finally { saveDesktop.disabled = !consent.checked; }
      });
      saveDesktop.disabled = true;
      consent.addEventListener("change", () => { saveDesktop.disabled = !consent.checked; });
      const syncActions = element("div", "workspace-dialog-actions"); syncActions.append(loadDesktop, saveDesktop);
      body.append(syncStatus, consentLabel, syncActions);
      size.focus();
    }
    function shortcuts() {
      if (!open("Keyboard shortcuts")) return;
      modal.onkeydown = null;
      const table = element("dl", "workspace-shortcuts");
      for (const [action, keys] of [["New tab", "Ctrl + Alt + T"], ["Search tabs", "Ctrl + Shift + K"], ["Launch Codex in directories", "Ctrl + Shift + P"], ["Find in terminal", "Ctrl + Shift + F"], ["Switch to tab 1–9", "Alt + 1–9"], ["Previous / next tab", "Ctrl + Page Up / Down"], ["Rename tab", "Double-click tab"], ["Tab actions", "Right-click tab"], ["Close menu or dialog", "Escape"]]) {
        table.append(element("dt", "", action), element("dd", "", keys));
      }
      body.append(table, element("p", "subtle", "Other keys are sent to the terminal. Browser-reserved shortcuts may vary by operating system."));
      close.focus();
    }
    const searchButton = button("⌕", () => searchTabs());
    searchButton.className = "workspace-tool-button";
    searchButton.title = "Search tabs (Ctrl+Shift+K)";
    searchButton.setAttribute("aria-label", "Search tabs");
    toolbar.append(searchButton);
    const handlers = [["Show/hide Tabs pane", () => toggleSidebar()], ["Search tabs", searchTabs], ["Hidden tabs", hiddenTabs], ["Launch Codex in directories", commandPalette], ["Find in terminal", find], ["Keyboard shortcuts", shortcuts]];
    window.addEventListener("keydown", event => {
      if (event.defaultPrevented || !event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey || root.querySelector("dialog[open]")) return;
      const action = { k: searchTabs, p: commandPalette, f: find }[event.key.toLowerCase()];
      if (action) { event.preventDefault(); event.stopPropagation(); action(); }
    }, true);
    return {
      updateTabs,
      menuItems: () => [button("Settings · themes and fonts", appearance), ...management.menuItems(), ...handlers.map(([name, action]) => button(name, action))],
      isOpen: () => modal.open,
    };
  }
  return { attachWorkspace, preferences, applyChrome };
})();

window.T1000Management = (() => {
  let api, workspace, dialog, body, heading, notice, opener, busy = false, dialogGeneration = 0;
  const el = (tag, cls = "", value) => {
    const node = document.createElement(tag); node.className = cls;
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const button = (name, action, cls = "") => {
    const node = el("button", cls, name); node.type = "button";
    node.addEventListener("click", () => {
      const started = dialogGeneration;
      Promise.resolve().then(action).catch(error => {
        if (!dialog.open && dialogGeneration !== started) return;
        if (!dialog.open) open("Action unavailable");
        report(error.message);
      });
    });
    return node;
  };
  function report(message) { notice.textContent = message; notice.hidden = !message; }
  function open(title, wide = false) {
    if (busy) return false;
    workspace?.menu && (workspace.menu.open = false);
    if (!dialog.open) {
      if (document.querySelector("dialog[open]")) return false;
      opener = document.activeElement;
      dialog.showModal();
    }
    dialog.classList.toggle("manager-wide", wide);
    heading.textContent = title; body.replaceChildren(); report("");
    dialogGeneration++;
    return true;
  }
  async function request(data) {
    const result = await api.post(data);
    if (!result || typeof result.ok !== "boolean") throw new Error("Invalid management response");
    if (result.error) throw new Error(result.error);
    return result;
  }
  async function run(data) {
    if (busy) throw new Error("Wait for the current action to finish.");
    busy = true; dialog.setAttribute("aria-busy", "true");
    report("Applying changes…");
    try { return await request(data); }
    finally { busy = false; dialog.removeAttribute("aria-busy"); }
  }
  async function navigate(windowId, sessionId, expectedName) {
    for (let attempt = 0; attempt < 24; attempt++) {
      const host = await api.snapshot();
      const win = host.windows.find(item => item.id === windowId && (!expectedName || item.name === expectedName));
      const pane = sessionId && win?.panes.find(item => item.sessionId === sessionId || item.id === sessionId);
      if (win && (!sessionId || pane)) {
        location.assign(api.appPath(win.path + (pane ? `/t/${encodeURIComponent(pane.id)}` : "")));
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error("Saved, but the live snapshot has not caught up. Refresh Windows; do not repeat the action.");
  }
  function field(label, control) {
    const row = el("label", "workspace-field");
    control.setAttribute("aria-label", label); row.append(el("span", "", label), control); return row;
  }
  function selector(values, value) {
    const select = el("select");
    for (const [id, label] of values) { const option = el("option", "", label); option.value = id; select.append(option); }
    if (value !== undefined) select.value = value;
    return select;
  }
  function nameWindow(rename = false) {
    const win = workspace?.getWindow();
    if (rename && !win || !open(rename ? "Rename window" : "New window")) return;
    const form = el("form", "management-form");
    const name = el("input"); name.required = true; name.maxLength = 100; name.value = rename ? win.name : "";
    const requestId = window.T1000Files.id();
    const submit = el("button", "primary", rename ? "Save name" : "Create window"); submit.type = "submit";
    form.append(field("Window name", name), el("p", "subtle", rename ? "Tabs and running processes stay in this window." : "Create an empty window, or configure initial remote tabs and staged client files."));
    let files, options, rootChoice, directoryPath, runtimeChoice, tabNames, task;
    if (!rename) {
      const configured = el("details"); configured.className = "new-window-configuration";
      configured.append(el("summary", "", "Configure workspace, runtime and client files"));
      const choices = el("div");
      const filesRoot = el("section"); filesRoot.setAttribute("aria-label", "Client files for new window");
      const loadChoices = async () => {
        if (!files?.machine || !configured.open) return;
        try {
          options = await window.T1000Files.management({operation: "launch-options"});
          const previousRoot = rootChoice?.value, previousRuntime = runtimeChoice?.value;
          const previousTabs = tabNames?.value, previousTask = task?.value;
          const previousDirectory = directoryPath?.value;
          rootChoice = selector([["", "Empty window (no initial tabs)"], ...(options.server_directory ? [["__server_directory__", "Enter server directory…"]] : []), ...options.workspaces.map(item => [item.id, `${item.label} · ${item.path}`])], previousRoot || "");
          directoryPath = el("input"); directoryPath.maxLength = 4096; directoryPath.spellcheck = false;
          directoryPath.autocomplete = "off"; directoryPath.placeholder = "/path/to/project"; directoryPath.value = previousDirectory || "";
          const directoryField = field("Server directory", directoryPath);
          const directoryHelp = el("p", "subtle", "Choose an existing folder on the TermDeck host. Files selected below are uploaded copies from this client machine.");
          const showDirectory = () => {
            const selected = rootChoice.value === "__server_directory__";
            directoryField.hidden = directoryHelp.hidden = !selected;
            directoryPath.disabled = !selected; directoryPath.required = selected;
          };
          rootChoice.addEventListener("change", showDirectory); showDirectory();
          runtimeChoice = selector(options.runtimes.map(item => [item.id, item.label]), previousRuntime || "shell");
          const runtimeHelp = el("p", "subtle", "Runs on the host as your user. The selected folder is the starting directory.");
          tabNames = el("textarea"); tabNames.value = previousTabs || "Main"; tabNames.maxLength = 1024;
          task = el("textarea"); task.maxLength = 8192; task.value = previousTask || ""; task.disabled = runtimeChoice.value !== "codex";
          runtimeChoice.addEventListener("change", () => { task.disabled = runtimeChoice.value !== "codex"; if (task.disabled) task.value = ""; });
          choices.replaceChildren(field("Server workspace", rootChoice), directoryField, directoryHelp, field("Runtime", runtimeChoice), runtimeHelp, field("Initial tab names (one per line, maximum 8)", tabNames), field("Codex task (optional)", task));
          if (!options.workspaces.length && !options.server_directory) choices.append(el("p", "subtle", "No server workspaces are available on this host yet. Empty windows remain available."));
        } catch (error) { report(error.message); }
      };
      let hadMachine = null;
      files = window.T1000Files.mount(filesRoot, {selectable: true, onChange: () => {
        const identity = files?.machine?.machine_id || null;
        if (identity !== hadMachine) { hadMachine = identity; if (identity) loadChoices(); else { options = null; rootChoice = null; choices.replaceChildren(); } }
      }});
      configured.addEventListener("toggle", () => { if (configured.open && !options) loadChoices(); });
      configured.append(choices, filesRoot); form.append(configured);
    }
    form.append(submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (busy) return;
      if (!name.value.trim()) { report("Window name is required."); return; }
      if (files?.busy) { report("Wait for the upload to finish before creating the window."); return; }
      submit.disabled = true;
      try {
        let result;
        if (!rename && rootChoice?.value) {
          const tabs = tabNames.value.split("\n").map(value => value.trim()).filter(Boolean);
          if (!tabs.length || tabs.length > 8) throw new Error("Enter between 1 and 8 tab names.");
          const workspaceSelection = rootChoice.value === "__server_directory__" ? {workspace_path: directoryPath.value} : {workspace_id: rootChoice.value};
          if (rootChoice.value === "__server_directory__" && !directoryPath.value) throw new Error("Enter an existing server directory.");
          busy = true;
          try { result = await window.T1000Files.management({operation: "create-window", request_id: requestId, policy_revision: options.policy_revision, name: name.value.trim(), ...workspaceSelection, runtime: runtimeChoice.value, tabs, task: task.value, attachments: files.references()}); }
          finally { busy = false; }
        } else {
          if (!rename && files?.references().length) throw new Error("Choose a server workspace to attach files to the new remote task.");
          result = await run({operation: rename ? "rename-window" : "create-window", name: name.value.trim(), ...(rename ? {window_id: win.id} : {request_id: requestId})});
        }
        await navigate(result.window_id, null, result.window_name || name.value.trim());
      } catch (error) { report(error.message); submit.disabled = false; }
    });
    body.append(form); name.focus(); name.select();
  }
  async function killWindow(windowId) {
    if (!windowId||!open("Kill window permanently")) return;
    const p=await request({operation:"preview-kill-window",window_id:windowId});
    if (!dialog.open || heading.textContent !== "Kill window permanently") return;
    const tabs=p.tabs;
    const warning = el("p","",`Kill “${p.window_name}” (${tabs.length} tabs)?`);
    const detail = el("p","subtle","Managed sessions stop. External references are forgotten; external tmux keeps running. Includes hidden tabs. Cannot be undone.");
    const list=el("ul","management-confirm-list");
    for (const t of tabs) list.append(el("li","",`${t.name} · ${t.status}${t.external ? " · external" : ""}`));
    const requestId = window.T1000Files.id(), controls = el("div","workspace-dialog-actions"), cancel = button("Cancel",()=>dialog.close());
    const confirm = button("Kill window permanently", async () => {
      confirm.disabled = true;
      try {
        const r = await run({operation:"kill-window",window_id:windowId,window_name:p.window_name,expected:p.expected,request_id:requestId});
        if (!r.ok) { report(`${r.remaining?.length||0} remain. ${resultsText(r)}`); return; }
        if (workspace?.getWindow()?.id === windowId) { location.assign(api.appPath("/")); return; }
        const host = await api.snapshot();
        if (host.windows.some(w=>w.id===windowId)) { report("Killed; refresh."); return; }
        api.root.querySelector(".window-list")?.removeAttribute("data-signature");
        window.T1000UI.renderHome(api.root,host,false,api.appPath); dialog.close();
      } catch (error) { report(error.message); }
      finally { confirm.disabled = false; }
    }, "danger");
    controls.append(cancel,confirm); body.replaceChildren(warning,detail,list,controls); cancel.focus();
  }
  async function moveTabs(rows) {
    if (!rows.length || !open("Move tabs")) return;
    body.append(el("p", "subtle", `Move ${rows.length} selected tab${rows.length === 1 ? "" : "s"}. Running processes and terminal contents are preserved.`));
    const state = await request({operation: "list", limit: 1});
    if (!dialog.open || heading.textContent !== "Move tabs") return;
    const destination = selector(state.windows.map(win => [win.id, win.name]));
    const move = button("Move tabs", async () => {
      move.disabled = true;
      try {
        await run({operation: "move", ids: rows.map(row => row.id), sources: Object.fromEntries(rows.map(row => [row.id, row.window_id])), window_id: destination.value});
        await navigate(destination.value, rows[0].id);
      } catch (error) { move.disabled = false; throw error; }
    }, "primary");
    body.append(field("Destination window", destination), move); destination.focus();
  }
  function resultsText(result) {
    const failed = (result.results || []).filter(row => !row.ok);
    return failed.length ? `${failed.length} action(s) failed. ${failed.map(row => `${row.id}: ${row.detail}`).join(" · ")}` : "Changes saved.";
  }
  async function bulk(action, rows, after) {
    if (!rows.length) throw new Error("Select at least one tab.");
    if (rows.length > 128) throw new Error("Select no more than 128 tabs at a time.");
    const ids = rows.map(row => row.id);
    if (["pause", "pause-hot", "kill", "remove", "archive", "hide"].includes(action)) {
      const confirm = el("dialog", "workspace-dialog management-confirm");
      const title = el("h2", "", `${action === "kill" ? "Stop processes" : action === "remove" ? "Remove saved records" : action === "pause-hot" ? "Pause hot processes" : action[0].toUpperCase() + action.slice(1)} — ${rows.length} tabs?`);
      title.id = "management-confirm-title"; confirm.setAttribute("aria-labelledby", title.id);
      const description = el("p", "subtle", action === "kill" ? "This stops the selected terminal processes. Pinned tabs are protected." : action === "remove" ? "Only confirmed stopped, unpinned records will be deleted. This cannot be undone." : "Only the tabs listed below are affected. Pinned tabs are protected from hide, archive and Pause hot.");
      const list = el("ul", "management-confirm-list"); for (const row of rows) list.append(el("li", "", row.name || row.title || row.id));
      const controls = el("div", "workspace-dialog-actions");
      const accepted = await new Promise(resolve => {
        const cancel = button("Cancel", () => confirm.close("cancel"));
        const accept = button("Confirm", () => confirm.close("confirm"), ["kill", "remove"].includes(action) ? "danger" : "primary");
        controls.append(cancel, accept); confirm.append(title, description, list, controls); document.body.append(confirm);
        window.T1000Backdrop(confirm);
        confirm.addEventListener("close", () => { const yes = confirm.returnValue === "confirm"; confirm.remove(); resolve(yes); }, {once: true});
        confirm.showModal(); cancel.focus();
      });
      if (!accepted) return;
    }
    const result = await run({operation: "bulk", action, ids, confirmation: ids});
    await workspace?.refresh();
    if (after) await after();
    report(resultsText(result));
  }
  function activeRows() {
    const win = workspace?.getWindow();
    if (!win) return [];
    const active = api.root.querySelector('[role="tab"][aria-selected="true"]')?.dataset.paneId;
    return win.panes.filter(pane => pane.id === active).map(pane => ({...pane, id: pane.sessionId || pane.id, name: pane.title, window_id: win.id}));
  }
  async function reorder(from, to) {
    const win = workspace.getWindow();
    if (!win || from === to || busy) return;
    const panes = win.panes, source = panes.findIndex(pane => pane.id === from), target = panes.findIndex(pane => pane.id === to);
    if (source < 0 || target < 0) return;
    if (panes[source].pinned !== panes[target].pinned) throw new Error("Pinned tabs stay together. Unpin the tab before moving it across the pin boundary.");
    const expected = panes.map(pane => pane.sessionId || pane.id), ids = [...expected];
    ids.splice(target, 0, ids.splice(source, 1)[0]);
    await run({operation: "reorder", window_id: win.id, ids, expected});
    for (let attempt = 0; attempt < 20; attempt++) {
      await workspace.refresh();
      if (workspace.getWindow()?.panes.map(pane => pane.sessionId || pane.id).join("\n") === ids.join("\n")) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("Order saved, but live refresh has not caught up yet.");
  }
  async function shiftTab(delta) {
    const win = workspace.getWindow(), active = activeRows()[0];
    if (!win || !active) return;
    const index = win.panes.findIndex(pane => (pane.sessionId || pane.id) === active.id);
    if (win.panes[index + delta]) await reorder(win.panes[index].id, win.panes[index + delta].id);
  }
  async function hideRelative(right) {
    const win = workspace.getWindow(), active = activeRows()[0];
    if (!win || !active || !open(right ? "Hide tabs to the right" : "Hide other tabs")) return;
    const index = win.panes.findIndex(pane => (pane.sessionId || pane.id) === active.id);
    const rows = win.panes.filter((pane, i) => i !== index && (!right || i > index) && !pane.pinned).map(pane => ({id: pane.sessionId || pane.id, name: pane.title}));
    body.append(el("p", "subtle", "Hiding tabs does not stop their processes. Reopen them from Hidden tabs or the manager."));
    await bulk("hide", rows);
  }
  async function sendSelected(rows, text, enter) {
    if (!rows.length || rows.length > 128) throw new Error("Select between 1 and 128 tabs.");
    if (!text || new TextEncoder().encode(text).length > 32000 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) throw new Error("Enter text without control codes, up to 32 KB.");
    const confirm = el("dialog", "workspace-dialog management-confirm");
    const title = el("h2", "", `Send ${enter ? "text + Enter" : "text"} to ${rows.length} terminals?`);
    title.id = "management-send-title"; confirm.setAttribute("aria-labelledby", title.id);
    const summary = el("p", "subtle", enter ? "This will submit the text in every listed terminal. It takes keyboard control for this action." : "Text is pasted into every listed terminal. No Enter key is sent, but newlines in the text may still execute commands. This takes keyboard control for the action.");
    const preview = el("pre", "manager-preview", text), list = el("ul", "management-confirm-list");
    for (const row of rows) list.append(el("li", "", `${row.window} / ${row.name}`));
    const controls = el("div", "workspace-dialog-actions");
    const accepted = await new Promise(resolve => {
      const cancel = button("Cancel", () => confirm.close("cancel"));
      controls.append(cancel, button("Send to selected terminals", () => confirm.close("send"), "primary"));
      confirm.append(title, summary, list, preview, controls); document.body.append(confirm);
      window.T1000Backdrop(confirm);
      confirm.addEventListener("close", () => { const accepted = confirm.returnValue === "send"; confirm.remove(); resolve(accepted); }, {once: true});
      confirm.showModal(); cancel.focus();
    });
    if (!accepted) return;
    if (busy) throw new Error("Wait for the current action to finish.");
    busy = true; dialog.setAttribute("aria-busy", "true");
    const results = [];
    try {
      const snapshot = await api.snapshot();
      for (const row of rows) {
        report(`Sending to ${results.length + 1} of ${rows.length}…`);
        try {
          const win = snapshot.windows.find(win => win.id === row.window_id);
          const pane = win?.panes.find(pane => (pane.sessionId || pane.id) === row.id);
          if (!pane || pane.status !== "running") throw new Error("Only visible, running terminals can receive text. Reopen or resume this tab first.");
          const base = `/_termdeck/api/v1/windows/${encodeURIComponent(win.slug)}/panes/${encodeURIComponent(pane.id)}`;
          let control = await api.postTerminal(base + "/control", {takeover: true});
          const checkedSequence = () => {
            if (!control.mine || control.indeterminate || !Number.isSafeInteger(control.next_sequence) || control.next_sequence < 1) throw new Error("Terminal input state is not ready; nothing will be retried.");
            return control.next_sequence;
          };
          control = await api.postTerminal(base + "/input/text", {sequence: checkedSequence(), text});
          if (enter) control = await api.postTerminal(base + "/input/key", {sequence: checkedSequence(), key: "enter"});
          checkedSequence();
          await api.postTerminal(base + "/control/release", {});
          results.push({id: row.id, ok: true, detail: "sent"});
        } catch (error) {
          // Do not retry uncertain input or clear its fence through a CLI send.
          results.push({id: row.id, ok: false, detail: `Delivery failed or is uncertain; not retried. ${error.message}`});
        }
      }
      report(results.every(row => row.ok) ? `Sent to ${results.length} terminal(s).` : resultsText({results}));
    } finally { busy = false; dialog.removeAttribute("aria-busy"); }
  }
  async function manager() {
    if (!open("Tab manager", true)) return;
    const filters = el("div", "manager-filters");
    const search = el("input"); search.type = "search"; search.placeholder = "Search name, window, command or directory"; search.setAttribute("aria-label", "Search manager");
    const view = selector([["all", "All tabs"], ["visible", "Visible tabs"], ["hidden", "Hidden tabs"], ["history", "Stopped / archived"]]); view.setAttribute("aria-label", "Manager view");
    const scope = selector([["", "All windows"]]); scope.setAttribute("aria-label", "Manager window");
    const live = el("input"); live.type = "checkbox"; live.checked = true;
    const liveLabel = el("label", "workspace-check"); liveLabel.append(live, el("span", "", "Live updates"));
    filters.append(search, view, scope, button("Refresh", () => load()), liveLabel);
    const count = el("p", "manager-count"); count.setAttribute("role", "status");
    const actions = el("div", "manager-actions"); actions.setAttribute("aria-label", "Selected tab actions");
    const tableBox = el("div", "manager-table-scroll"); tableBox.tabIndex = 0; tableBox.setAttribute("aria-label", "Scrollable tab table");
    const table = el("table", "manager-table"), head = el("thead"), rowsBody = el("tbody");
    const header = el("tr"); const all = el("input"); all.type = "checkbox"; all.setAttribute("aria-label", "Select this page"); const checkHeader = el("th"); checkHeader.append(all); header.append(checkHeader);
    const columns = [["name", "Tab"], ["window", "Window"], ["pinned", "Pin"], ["pid", "PID"], ["state", "State"], ["health", "Health"], ["cpu_percent", "CPU %"], ["memory_bytes", "RAM"], ["process_count", "Procs"], ["runtime_seconds", "Runtime"], ["focus_key", "Focus key"], ["pause_key", "Pause key"], ["command", "Command"], ["cwd", "Directory"], ["id", "ID"]];
    let rows = [], selected = new Set(), offset = 0, total = 0, sort = "", descending = false, sequence = 0, debounce, timer, loading = false;
    for (const [key, label] of columns) {
      const th = el("th"); th.scope = "col";
      th.append(button(label, () => { descending = sort === key && !descending; sort = key; offset = 0; for (const cell of header.querySelectorAll("th")) cell.removeAttribute("aria-sort"); th.setAttribute("aria-sort", descending ? "descending" : "ascending"); return load(); }));
      header.append(th);
    }
    head.append(header); table.append(head, rowsBody); tableBox.append(table);
    const preview = el("pre", "manager-preview"); preview.hidden = true; preview.setAttribute("aria-label", "Terminal output preview");
    const composer = el("details", "manager-composer");
    composer.append(el("summary", "", "Send to selected terminals"));
    const text = el("textarea"); text.rows = 2; text.maxLength = 16000; text.setAttribute("aria-label", "Text for selected terminals"); text.placeholder = "Text for the selected tabs. Sending always asks for confirmation.";
    const sendActions = el("div", "workspace-dialog-actions");
    sendActions.append(button("Send text", () => sendSelected(selection(), text.value, false)), button("Send + Enter", () => sendSelected(selection(), text.value, true)), button("Status request", () => sendSelected(selection(), "Pause and summarise your current state in five concise lines. Do not modify files.", true)));
    composer.append(text, sendActions);
    const footer = el("div", "manager-pagination");
    const previous = button("Previous", () => { offset = Math.max(0, offset - 100); return load(); });
    const next = button("Next", () => { offset += 100; return load(); });
    footer.append(previous, next, el("span", "subtle", "100 rows per page · column headers sort all matching tabs"));
    function selection() { return rows.filter(row => selected.has(row.id)); }
    const selectionButtons = [];
    const addAction = (label, action, single = false, cls = "") => {
      const control = button(label, action, cls); control.dataset.single = String(single); selectionButtons.push(control); actions.append(control);
    };
    addAction("Open", async () => {
      const row = selection()[0];
      if (!row.visible) { const result = await run({operation: "bulk", action: "show", ids: [row.id]}); if (!result.ok) throw new Error(resultsText(result)); }
      await navigate(row.window_id, row.id);
    }, true);
    addAction("Rename", () => {
      body.querySelector(".manager-inline-editor")?.remove();
      const row = selection()[0]; const editor = el("form", "manager-inline-editor"); const input = el("input"); input.value = row.name; input.required = true; input.maxLength = 100;
      const submit = el("button", "primary", "Save tab name"); submit.type = "submit";
      editor.append(field("Tab name", input), submit, button("Cancel rename", () => editor.remove())); preview.before(editor); input.focus(); input.select();
      editor.addEventListener("submit", async event => { event.preventDefault(); if (busy) return; submit.disabled = true; try { await run({operation: "rename", ids: [row.id], name: input.value}); editor.remove(); await load(); await workspace?.refresh(); report("Tab renamed."); } catch (error) { report(error.message); submit.disabled = false; } });
    }, true);
    addAction("Preview", async () => {
      const id = selection()[0].id; preview.hidden = false; preview.dataset.sessionId = id; preview.textContent = "Loading…";
      const data = await request({operation: "capture", ids: [id]});
      if (selected.size === 1 && selected.has(id) && preview.isConnected) { preview.hidden = false; preview.textContent = data.capture || "No captured output."; }
    }, true);
    addAction("Move…", () => moveTabs(selection()));
    for (const [action, label] of [["pause", "Pause"], ["resume", "Resume"], ["pin", "Pin"], ["unpin", "Unpin"], ["hide", "Hide"], ["show", "Reopen"], ["archive", "Archive"], ["pause-hot", "Pause hot"], ["kill", "Kill…"], ["remove", "Remove…"]]) addAction(label, () => bulk(action, selection(), load), false, action === "kill" || action === "remove" ? "danger" : "");
    function updateSelection() {
      if (preview.dataset.sessionId && (selected.size !== 1 || !selected.has(preview.dataset.sessionId))) preview.hidden = true;
      count.textContent = `${total ? offset + 1 : 0}–${offset + rows.length} of ${total} tabs · ${selected.size} selected`;
      all.checked = rows.length > 0 && selected.size === rows.length; all.indeterminate = selected.size > 0 && selected.size < rows.length;
      for (const control of selectionButtons) control.disabled = !selected.size || (control.dataset.single === "true" && selected.size !== 1);
    }
    function render() {
      const ordered = [...rows];
      if (sort) ordered.sort((a, b) => (typeof a[sort] === "number" ? a[sort] - b[sort] : String(a[sort] ?? "").localeCompare(String(b[sort] ?? ""))) * (descending ? -1 : 1));
      rowsBody.replaceChildren();
      for (const row of ordered) {
        const tr = el("tr"); tr.dataset.sessionId = row.id;
        const check = el("input"); check.type = "checkbox"; check.checked = selected.has(row.id); check.setAttribute("aria-label", `Select ${row.name}`);
        check.addEventListener("change", () => { check.checked ? selected.add(row.id) : selected.delete(row.id); updateSelection(); });
        const td = el("td"); td.append(check); tr.append(td);
        for (const [key] of columns) {
          const value = key === "memory_bytes" ? row.memory : key === "runtime_seconds" ? row.runtime : key === "pinned" ? row.pinned ? "◆" : "" : row[key] ?? "—";
          const cell = el("td", "", value); cell.title = String(value); tr.append(cell);
        }
        tr.addEventListener("dblclick", () => navigate(row.window_id, row.id).catch(error => report(error.message)));
        rowsBody.append(tr);
      }
      updateSelection(); previous.disabled = offset === 0; next.disabled = offset + rows.length >= total;
    }
    all.addEventListener("change", () => { selected = all.checked ? new Set(rows.map(row => row.id)) : new Set(); render(); });
    async function load(automatic = false) {
      if (automatic && (loading || busy || !live.checked || document.hidden || body.querySelector(".manager-inline-editor"))) return;
      const generation = ++sequence;
      loading = true;
      for (const control of selectionButtons) control.disabled = true;
      if (!automatic) count.textContent = "Loading tabs…";
      let data;
      try { data = await request({operation: "list", query: search.value, view: view.value, offset, limit: 100, sort, descending, ...(scope.value ? {window_id: scope.value} : {})}); }
      finally { if (generation === sequence) { loading = false; updateSelection(); } }
      if (generation !== sequence || !dialog.open || heading.textContent !== "Tab manager") return;
      if (data.total && offset >= data.total) { offset = 0; return load(); }
      const focused = document.activeElement?.closest("tr")?.dataset.sessionId;
      rows = data.rows; total = data.total;
      selected = new Set([...selected].filter(id => rows.some(row => row.id === id)));
      const oldScope = scope.value; scope.replaceChildren(...selector([["", "All windows"], ...data.windows.map(win => [win.id, win.name])]).children); scope.value = oldScope;
      render();
      if (focused) [...rowsBody.querySelectorAll("tr")].find(row => row.dataset.sessionId === focused)?.querySelector("input")?.focus();
    }
    const reload = () => { offset = 0; load().catch(error => report(error.message)); };
    search.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(reload, 180); });
    view.addEventListener("change", reload); scope.addEventListener("change", reload);
    body.append(filters, count, actions, tableBox, footer, preview, composer); await load(); search.focus();
    const tick = async () => {
      if (!dialog.open || heading.textContent !== "Tab manager" || !filters.isConnected) return;
      try { await load(true); } catch (error) { report(error.message); }
      timer = setTimeout(tick, 5000);
    };
    timer = setTimeout(tick, 5000);
    dialog.addEventListener("close", () => { clearTimeout(timer); clearTimeout(debounce); sequence++; }, {once: true});
  }
  function configure(value) {
    api = value;
    window.T1000Files.configure(value);
    window.T1000UI.applyChrome();
    dialog = el("dialog", "workspace-dialog management-dialog"); dialog.id = "management-dialog";
    heading = el("h2"); heading.id = "management-title"; dialog.setAttribute("aria-labelledby", heading.id);
    const header = el("div", "workspace-tools-heading"); header.append(heading, button("Close", () => { if (!busy && !dialog.querySelector('[data-uploading="true"]')) dialog.close(); }));
    body = el("div", "management-body"); notice = el("p", "management-notice"); notice.setAttribute("role", "status"); notice.hidden = true;
    dialog.append(header, notice, body); document.body.append(dialog);
    window.T1000Backdrop(dialog, () => !busy && !dialog.querySelector('[data-uploading="true"]'));
    dialog.addEventListener("cancel", event => { if (busy || dialog.querySelector('[data-uploading="true"]')) event.preventDefault(); });
    dialog.addEventListener("close", () => { dialogGeneration++; if (opener?.isConnected) opener.focus(); });
  }
  function home(header) {
    const tools = el("div", "window-list-tools"); tools.append(button("New window", () => nameWindow(), "primary"), button("Client files", clientFiles), button("Codex accounts", accounts), button("Tab manager", manager)); header.append(tools);
  }
  function clientFiles() { if (open("Client files", true)) window.T1000Files.mount(body); }
  function accounts() { if (open("Codex accounts", true)) window.T1000Accounts.mount(body, request); }
  function attachWorkspace(value) {
    workspace = value;
    const managerButton = button("▤", manager, "workspace-tool-button"); managerButton.title = "Tab manager"; managerButton.setAttribute("aria-label", "Tab manager"); value.toolbar.append(managerButton);
    const title = value.root.querySelector(".workspace-title"); title.tabIndex = 0; title.setAttribute("role", "button"); title.title = "Rename window"; title.setAttribute("aria-label", "Rename window");
    title.addEventListener("click", () => nameWindow(true)); title.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); nameWindow(true); } });
    const bar = value.root.querySelector(".workspace-tabbar"); let dragged = "";
    const decorate = () => { for (const tab of bar.querySelectorAll('[role="tab"]')) { tab.draggable = true; tab.title += tab.title.includes("Drag to reorder") ? "" : "\nDrag to reorder · Ctrl+Shift+Page Up/Down"; } };
    new MutationObserver(decorate).observe(bar, {childList: true}); decorate();
    const error = error => { if (open("Tab action")) report(error.message); };
    bar.addEventListener("dragstart", event => { dragged = event.target.closest('[role="tab"]')?.dataset.paneId || ""; if (dragged) { event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", dragged); } });
    bar.addEventListener("dragover", event => { if (dragged && event.target.closest('[role="tab"]')) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } });
    bar.addEventListener("drop", event => { if (!dragged) return; event.preventDefault(); const target = event.target.closest('[role="tab"]')?.dataset.paneId; const from = dragged; dragged = ""; if (target) reorder(from, target).catch(error); });
    bar.addEventListener("dragend", () => { dragged = ""; });
    window.addEventListener("keydown", event => { if (!event.ctrlKey || !event.shiftKey || !["PageUp", "PageDown"].includes(event.key) || document.querySelector("dialog[open]")) return; event.preventDefault(); event.stopImmediatePropagation(); shiftTab(event.key === "PageUp" ? -1 : 1).catch(error); }, true);
    const handlers = [["New window", () => nameWindow()], ["Client files", clientFiles], ["Codex accounts", accounts], ["Rename window", () => nameWindow(true)], ["Kill window permanently…", () => killWindow(workspace?.getWindow()?.id)], ["Tab manager", manager], ["Move tab to window…", () => moveTabs(activeRows())], ["Move tab left", () => shiftTab(-1)], ["Move tab right", () => shiftTab(1)], ["Hide other tabs", () => hideRelative(false)], ["Hide tabs to the right", () => hideRelative(true)]];
    return {menuItems: () => handlers.map(([name, handler]) => button(name, handler))};
  }
  return {configure, home, workspace: attachWorkspace, killWindow, appearance: data => request({operation: "appearance", ...data})};
})();
