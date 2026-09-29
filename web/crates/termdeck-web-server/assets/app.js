"use strict";

window.T1000Backdrop = (dialog, mayClose = () => true) => {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog || !mayClose()) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
};

(() => {
  const root = document.getElementById("termdeck-app");
  if (!root || typeof window.fetch !== "function") {
    return;
  }

  const MAX_WINDOWS = 4096;
  const MAX_PANES = 16384;
  const MAX_ID_CHARS = 256;
  const MAX_NAME_CHARS = 2048;
  const MAX_CWD_CHARS = 8192;
  const REFRESH_VISIBLE_MS = 2000;
  const REFRESH_HIDDEN_MS = 15000;
  const REQUEST_TIMEOUT_MS = 5000;
  const STATUS_NAMES = new Set([
    "new",
    "running",
    "paused",
    "missing",
    "recovery-pending",
    "start-error",
    "dead",
    "stopped",
    "archived",
    "unknown",
  ]);

  const page = root.getAttribute("data-termdeck-page");
  const routeSlug = root.getAttribute("data-termdeck-slug") || "";
  const routePane = root.getAttribute("data-termdeck-pane") || "";
  const rawBasePath = root.getAttribute("data-termdeck-base") || "";
  const basePath = rawBasePath === "" || (
    rawBasePath.length <= 128
    && rawBasePath.startsWith("/")
    && !rawBasePath.endsWith("/")
    && /^\/[a-z0-9_-]+(?:\/[a-z0-9_-]+)*$/.test(rawBasePath)
  ) ? rawBasePath : "";

  function appPath(path) {
    if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
      throw new Error("Invalid application path.");
    }
    return `${basePath}${path}`;
  }

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function requiredString(value, maximum, field) {
    if (typeof value !== "string" || value.length > maximum) {
      throw new Error(`Invalid ${field}.`);
    }
    return value;
  }

  function optionalString(value, maximum, field) {
    if (value === null || value === undefined) {
      return null;
    }
    return requiredString(value, maximum, field);
  }

  function nonNegativeInteger(value, field) {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error(`Invalid ${field}.`);
    }
    return value;
  }

  function exactRevision(value, field) {
    if (
      typeof value.revision_text === "string" &&
      /^(?:0|[1-9][0-9]{0,19})$/.test(value.revision_text)
    ) {
      return value.revision_text;
    }
    return String(nonNegativeInteger(value.revision, field));
  }

  function boundedArray(value, maximum, field) {
    if (!Array.isArray(value) || value.length > maximum) {
      throw new Error(`Invalid ${field}.`);
    }
    return value;
  }

  function validSlug(value) {
    return (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= 64 &&
      value !== "_termdeck" &&
      !value.includes("--") &&
      /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(value)
    );
  }

  function normalizeController(value) {
    if (value === null || value === undefined) {
      return false;
    }
    if (!isRecord(value)) {
      throw new Error("Invalid pane controller state.");
    }
    nonNegativeInteger(value.epoch, "controller epoch");
    nonNegativeInteger(value.expires_at_unix_ms, "controller expiry");
    return true;
  }

  function normalizePane(value) {
    if (!isRecord(value)) {
      throw new Error("Invalid pane.");
    }
    const status = requiredString(value.status, 64, "pane status");
    if (!STATUS_NAMES.has(status)) {
      throw new Error("Unknown pane status.");
    }
    return {
      id: requiredString(value.id, MAX_ID_CHARS, "pane id"),
      sessionId: optionalString(value.session_id, MAX_ID_CHARS, "session id"),
      title: requiredString(value.title, MAX_NAME_CHARS, "pane title"),
      cwd: requiredString(value.cwd, MAX_CWD_CHARS, "pane cwd"),
      status,
      pinned: value.pinned === true,
      automaticTitle: value.automatic_title === true,
      controllerActive: normalizeController(value.controller),
    };
  }

  function normalizeWindow(value) {
    if (!isRecord(value)) {
      throw new Error("Invalid window.");
    }
    const slug = requiredString(value.slug, 64, "window slug");
    if (!validSlug(slug)) {
      throw new Error("Invalid window slug.");
    }
    const path = requiredString(value.path, 66, "window path");
    if (path !== `/${slug}`) {
      throw new Error("Window path does not match its slug.");
    }
    return {
      id: requiredString(value.id, MAX_ID_CHARS, "window id"),
      name: requiredString(value.name, MAX_NAME_CHARS, "window name"),
      slug,
      path,
      activePaneId: optionalString(value.active_pane_id, MAX_ID_CHARS, "active pane id"),
      panes: boundedArray(value.panes, MAX_PANES, "window panes").map(normalizePane),
    };
  }

  function normalizeHost(value) {
    if (!isRecord(value) || value.schema !== "termdeck.host-snapshot.v1") {
      throw new Error("Unsupported host snapshot schema.");
    }
    const windows = boundedArray(value.windows, MAX_WINDOWS, "host windows").map(
      normalizeWindow,
    );
    const totalPanes = nonNegativeInteger(value.total_panes, "total pane count");
    const observedPanes = windows.reduce((total, windowData) => total + windowData.panes.length, 0);
    if (observedPanes !== totalPanes || observedPanes > MAX_PANES) {
      throw new Error("Host pane count is inconsistent.");
    }
    return {
      revision: exactRevision(value, "host revision"),
      generatedAt: nonNegativeInteger(value.generated_at_unix_ms, "snapshot time"),
      totalPanes,
      windows,
    };
  }

  function normalizeWindowEnvelope(value) {
    if (!isRecord(value) || value.schema !== "termdeck.window-snapshot.v1") {
      throw new Error("Unsupported window snapshot schema.");
    }
    return {
      revision: exactRevision(value, "window revision"),
      generatedAt: nonNegativeInteger(value.generated_at_unix_ms, "snapshot time"),
      window: normalizeWindow(value.window),
    };
  }

  function node(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = String(text);
    }
    return element;
  }

  function link(text, href, className) {
    const element = node("a", className, text);
    element.setAttribute("href", href);
    return element;
  }

  function notice(text, kind) {
    const element = node("div", `notice ${kind || ""}`.trim(), text);
    const urgent = kind === "notice-error";
    element.setAttribute("role", urgent ? "alert" : "status");
    element.setAttribute("aria-live", urgent ? "assertive" : "polite");
    return element;
  }

  function renderHome(value, stale) {
    const host = normalizeHost(value);
    window.T1000UI.renderHome(root, host, stale, appPath);
    root.dataset.termdeckState = stale ? "stale" : "ready";
    root.setAttribute("aria-busy", "false");
    document.title = "t1000";
  }

  function currentClientNotice() {
    return root.querySelector("[data-termdeck-client-notice]");
  }

  function showClientNotice(message, kind) {
    let element = currentClientNotice();
    if (!element) {
      element = notice(message, kind);
      element.setAttribute("data-termdeck-client-notice", "1");
      root.insertBefore(element, root.firstChild);
      return;
    }
    element.className = `notice ${kind}`;
    element.textContent = message;
    const urgent = kind === "notice-error";
    element.setAttribute("role", urgent ? "alert" : "status");
    element.setAttribute("aria-live", urgent ? "assertive" : "polite");
  }

  function clearClientNotice() {
    const element = currentClientNotice();
    if (element) {
      element.remove();
    }
  }

  function errorMessage(error) {
    if (error instanceof Error && error.message) {
      const explanations = {
        "tab-action-failed": "The server could not complete this action. Its t1000 launcher may need repair. Your tab has not been removed from this view.",
        "tab-action-timeout": "The server took too long to respond. Check the tab before trying again; the action may still have completed.",
        "tab-action-busy": "Other tab actions are still running. Wait for them to finish, then try again.",
        "tab-action-internal": "The tab service is unavailable. Check the server before trying again.",
      };
      if (explanations[error.message]) {
        return explanations[error.message];
      }
      return error.message.slice(0, 240);
    }
    return "Request failed.";
  }

  function cookieValue(names) {
    for (const item of document.cookie.split(";")) {
      const trimmed = item.trim();
      const separator = trimmed.indexOf("=");
      if (separator <= 0) {
        continue;
      }
      const name = trimmed.slice(0, separator);
      if (names.includes(name)) {
        return decodeURIComponent(trimmed.slice(separator + 1));
      }
    }
    return "";
  }

  async function fetchJson(url, options) {
    const configuredTimeout = isRecord(options) && Number.isSafeInteger(options.timeoutMs)
      ? Math.min(180000, Math.max(1000, options.timeoutMs))
      : REQUEST_TIMEOUT_MS;
    const requestOptions = isRecord(options) ? { ...options } : {};
    delete requestOptions.timeoutMs;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), configuredTimeout);
    try {
      const response = await window.fetch(url, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
        ...requestOptions,
      });
      if (response.redirected && new URL(response.url).pathname === appPath("/_termdeck/login")) {
        window.location.assign(response.url);
        throw new Error("Authentication expired.");
      }
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("application/json") ? await response.json() : null;
      if (!response.ok) {
        const detail = isRecord(payload) && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      return { payload, response };
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function mutateJson(url, payload, timeoutMs) {
    const csrf = cookieValue(["termdeck_csrf", "__Host-termdeck_csrf"]);
    if (!csrf) {
      throw new Error("CSRF session capability is unavailable. Sign in again.");
    }
    return fetchJson(url, {
      body: JSON.stringify(payload || {}),
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-TermDeck-CSRF": csrf,
      },
      method: "POST",
      timeoutMs,
    });
  }

  window.T1000Management.configure({root, appPath,
    postTerminal: async (path, data) => (await mutateJson(appPath(path), data, 10000)).payload,
    post: async data => (await mutateJson(appPath("/_termdeck/api/v1/management"), data, 180000)).payload,
    snapshot: async () => normalizeHost((await fetchJson(appPath("/_termdeck/api/v1/snapshot"))).payload),
  });

  function startSnapshotRefresh() {
    const endpoint = appPath("/_termdeck/api/v1/snapshot");
    let requestInFlight = false;
    let refreshTimer = 0;
    let failureCount = 0;

    function nextDelay() {
      if (document.hidden) {
        return REFRESH_HIDDEN_MS;
      }
      if (failureCount === 0) {
        return REFRESH_VISIBLE_MS;
      }
      return Math.min(30000, 1000 * 2 ** Math.min(failureCount, 5));
    }

    function scheduleRefresh() {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(refresh, nextDelay());
    }

    async function refresh() {
      if (requestInFlight) {
        return;
      }
      requestInFlight = true;
      root.dataset.termdeckState = "refreshing";
      root.setAttribute("aria-busy", "true");
      try {
        const { payload, response } = await fetchJson(endpoint, {
          headers: { Accept: "application/json" },
          method: "GET",
        });
        const stale = response.headers.get("x-termdeck-stale") === "1";
        renderHome(payload, stale);
        failureCount = 0;
        if (!stale) {
          clearClientNotice();
        }
      } catch (error) {
        failureCount = Math.min(failureCount + 1, 6);
        root.dataset.termdeckState = "error";
        root.setAttribute("aria-busy", "false");
        showClientNotice(`Live refresh unavailable. ${errorMessage(error)} Retrying.`, "notice-error");
      } finally {
        requestInFlight = false;
        scheduleRefresh();
      }
    }

    document.addEventListener("visibilitychange", scheduleRefresh);
    refresh();
  }

  function mountTerminal(...args) {
    return window.T1000TerminalSession.mount(...args, { appPath, isRecord, nonNegativeInteger, errorMessage });
  }

  function startWorkspace(initialPaneId) {
    if (!validSlug(routeSlug)) {
      return;
    }
    const library = window.TermDeckTerminal;
    if (!library || typeof library.createTerminal !== "function") {
      showClientNotice("The interactive terminal assets did not load.", "notice-error");
      return;
    }

    document.body.classList.add("workspace-page");
    root.className = "workspace-main";
    const topbar = node("header", "workspace-headerbar");
    const leftTools = node("div", "workspace-header-left");
    const homeLink = link("t1000", appPath("/"), "workspace-home");
    homeLink.title = "All t1000 windows";
    const newButton = node("button", "workspace-new-tab", "+");
    newButton.type = "button";
    newButton.title = "New tab in the home directory";
    newButton.setAttribute("aria-label", "New tab");
    leftTools.append(homeLink, newButton);
    const titleGroup = node("div", "workspace-title-group");
    const windowTitle = node("strong", "workspace-title", "Loading…");
    titleGroup.append(windowTitle);
    const rightTools = node("div", "workspace-header-right");
    const workspaceStatus = node("span", "workspace-status", "Connecting…");
    const terminalStatus = node("span", "terminal-status", "Connecting…");
    terminalStatus.id = "terminal-status";
    terminalStatus.setAttribute("role", "status");
    terminalStatus.setAttribute("aria-live", "polite");
    const actionMenu = document.createElement("details");
    actionMenu.className = "workspace-actions";
    const actionSummary = node("summary", "", "⋯");
    actionSummary.title = "Tab actions";
    actionSummary.setAttribute("aria-label", "Tab actions");
    const actionPanel = node("div", "workspace-action-panel");
    actionMenu.append(actionSummary, actionPanel);
    rightTools.append(workspaceStatus, actionMenu);
    topbar.append(leftTools, titleGroup, rightTools);

    const tabbar = node("div", "workspace-tabbar");
    tabbar.setAttribute("role", "tablist");
    tabbar.setAttribute("aria-label", "t1000 tabs");
    tabbar.append(node("span", "workspace-tabs-loading", "Syncing tabs…"));
    const shell = node("section", "workspace-shell");
    shell.dataset.state = "loading";
    const shellStatus = node("p", "workspace-state-message", "Syncing t1000…");
    shellStatus.setAttribute("role", "status");
    shellStatus.setAttribute("aria-live", "polite");
    const terminalIdentity = node("span", "workspace-terminal-identity", "No tab selected");
    const statusbar = node("footer", "workspace-statusbar");
    const shortcutHint = node("span", "workspace-shortcut-hint", "New tab  Ctrl+Alt+T");
    statusbar.append(terminalStatus, terminalIdentity, shortcutHint);
    let xtermShell = node("div", "xterm-shell");
    xtermShell.id = "terminal-shell";
    let terminalHost = node("div", "xterm-host");
    terminalHost.id = "terminal-xterm";
    terminalHost.setAttribute("data-termdeck-terminal", "1");
    let terminalOverlay = node("div", "terminal-overlay");
    terminalOverlay.id = "terminal-overlay";
    terminalOverlay.hidden = true;
    xtermShell.append(terminalHost, terminalOverlay);
    const emptyNewButton = node("button", "workspace-empty-action", "Create a new tab");
    emptyNewButton.type = "button";
    emptyNewButton.addEventListener("click", () => newButton.click());
    const actionNotice = node("div", "workspace-action-notice");
    actionNotice.hidden = true;
    const actionNoticeText = node("p", "");
    actionNoticeText.setAttribute("role", "alert");
    const dismissNotice = node("button", "", "Dismiss");
    dismissNotice.type = "button";
    dismissNotice.addEventListener("click", () => {
      actionNotice.hidden = true;
      terminalMount?.focus();
    });
    actionNotice.append(actionNoticeText, dismissNotice);
    shell.append(shellStatus, emptyNewButton, xtermShell, actionNotice);

    function labeled(labelText, input) {
      const label = node("label", "workspace-field");
      label.append(node("span", "", labelText), input);
      return label;
    }

    const actionDialog = document.createElement("dialog");
    actionDialog.id = "workspace-action-dialog";
    actionDialog.className = "workspace-dialog";
    const actionForm = document.createElement("form");
    actionForm.method = "dialog";
    const actionTitle = node("h2", "");
    actionTitle.id = "workspace-action-title";
    const actionDescription = node("p", "workspace-dialog-description");
    actionDescription.id = "workspace-action-description";
    actionDialog.setAttribute("aria-labelledby", actionTitle.id);
    actionDialog.setAttribute("aria-describedby", actionDescription.id);
    const renameInput = document.createElement("input");
    renameInput.name = "rename";
    renameInput.autocomplete = "off";
    renameInput.maxLength = 256;
    const renameField = labeled("Tab name", renameInput);
    const actionCancel = node("button", "secondary", "Cancel");
    actionCancel.type = "button";
    const actionConfirm = node("button", "", "Confirm");
    actionConfirm.type = "submit";
    const actionButtons = node("div", "workspace-dialog-actions");
    actionButtons.append(actionCancel, actionConfirm);
    actionForm.append(actionTitle, actionDescription, renameField, actionButtons);
    actionDialog.append(actionForm);
    for (const modal of [actionDialog]) {
      window.T1000Backdrop(modal);
      modal.addEventListener("keydown", (event) => {
        if (event.key !== "Tab") return;
        const controls = [...modal.querySelectorAll("input:not(:disabled), button:not(:disabled)")];
        const edge = event.shiftKey ? controls[0] : controls.at(-1);
        if (document.activeElement === edge) {
          event.preventDefault();
          (event.shiftKey ? controls.at(-1) : controls[0]).focus();
        }
      });
    }
    let finishActionDialog = null;
    let dialogTrigger = null;
    actionCancel.addEventListener("click", () => actionDialog.close());
    actionForm.addEventListener("submit", (event) => {
      event.preventDefault();
      actionDialog.close("confirm");
    });
    renameInput.addEventListener("input", () => renameInput.setCustomValidity(""));
    actionDialog.addEventListener("close", () => {
      const resolve = finishActionDialog;
      finishActionDialog = null;
      if (dialogTrigger?.isConnected) {
        dialogTrigger.focus();
      } else {
        actionSummary.focus();
      }
      resolve?.(actionDialog.returnValue === "confirm" ? renameInput.value.trim() : null);
    });

    function askTabAction(action, pane) {
      const rename = action === "rename";
      actionTitle.textContent = rename ? "Rename tab" : "Kill process and close tab?";
      actionDescription.textContent = rename
        ? "Give this tab a name you can find quickly, or leave it empty to name it after its working directory. Its process will keep running."
        : `This stops “${pane.title}” and removes its tab. Unsaved terminal work may be lost. To keep the process running, cancel and choose Hide tab instead.`;
      renameField.hidden = !rename;
      renameInput.disabled = !rename;
      renameInput.value = pane.automaticTitle ? "" : pane.title;
      renameInput.placeholder = pane.title;
      renameInput.setCustomValidity("");
      actionConfirm.textContent = rename ? "Save name" : "Kill process";
      actionConfirm.className = rename ? "" : "danger";
      actionDialog.returnValue = "";
      dialogTrigger = actionMenu.open ? actionSummary : document.activeElement;
      actionMenu.open = false;
      return new Promise((resolve) => {
        finishActionDialog = resolve;
        actionDialog.showModal();
        if (rename) {
          renameInput.focus();
          renameInput.select();
        } else {
          actionCancel.focus();
        }
      });
    }

    root.replaceChildren(topbar, tabbar, shell, statusbar, actionDialog);

    let envelope = null;
    let activePaneId = initialPaneId || "";
    let terminalMount = null;
    const terminalViews = window.T1000TerminalSession.createCache({
      parent: shell, before: actionNotice,
      initial: { shell: xtermShell, host: terminalHost, overlay: terminalOverlay },
      create: view => mountTerminal(routeSlug, view.paneId, view.host, terminalStatus, view.overlay),
    });
    let refreshTimer = 0;
    let refreshInFlight = false;
    let refreshRequested = false;
    let disposed = false;
    let lastTabSignature = "";
    let lastActionSignature = "";
    let pendingAction = "";
    const workspaceTools = window.T1000UI.attachWorkspace({
      root, toolbar: leftTools, menu: actionMenu,
      getPanes: () => envelope?.window.panes || [],
      selectPane: (id) => switchPane(id, true),
      getTerminal: () => terminalHost.termdeckTerminal,
      newTab: () => newButton.click(), runAction: runTabAction,
      getWindow: () => envelope?.window, refresh: () => refreshWindow(),
      snapshot: async () => normalizeHost((await fetchJson(appPath("/_termdeck/api/v1/snapshot"))).payload), appPath,
      loadHidden: async () => (await fetchJson(appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/hidden-tabs`), { method: "GET" })).payload,
      restoreHidden: async (id) => {
        if (pendingAction) throw new Error("Wait for the current tab action to finish.");
        pendingAction = "restore";
        newButton.disabled = true;
        try {
          await mutateJson(appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/hidden-tabs/${encodeURIComponent(id)}/restore`), {}, 180000);
          if (!await waitForPane(id)) throw new Error("The tab was restored, but live refresh has not caught up. Do not retry yet.");
          switchPane(id, true);
        } finally {
          pendingAction = "";
          newButton.disabled = false;
          renderTabs();
          renderActions();
        }
      },
    });

    function updateShellState(state, message) {
      shell.dataset.state = state;
      const messages = {
        loading: "Syncing t1000…",
        empty: "No visible tabs",
        error: "Live refresh unavailable",
      };
      shellStatus.textContent = message || messages[state] || "";
      shellStatus.hidden = state === "ready" || state === "degraded";
    }

    function currentPane() {
      return envelope?.window.panes.find((pane) => pane.id === activePaneId) || null;
    }

    function panePath(paneId) {
      return appPath(`/${encodeURIComponent(routeSlug)}/t/${encodeURIComponent(paneId)}`);
    }

    function selectedFromLocation() {
      const prefix = appPath(`/${routeSlug}/t/`);
      if (!window.location.pathname.startsWith(prefix)) {
        return "";
      }
      try {
        return decodeURIComponent(window.location.pathname.slice(prefix.length));
      } catch (_error) {
        return "";
      }
    }

    function focusTabButton(paneId) {
      window.requestAnimationFrame(() => {
        for (const tab of tabbar.querySelectorAll("[role=tab]")) {
          if (tab.dataset.paneId === paneId) {
            tab.focus();
            return;
          }
        }
      });
    }

    function switchPane(paneId, updateHistory, focusTab = false) {
      if (typeof paneId !== "string" || paneId.length === 0 || paneId.length > MAX_ID_CHARS) {
        return;
      }
      if (envelope && !envelope.window.panes.some((pane) => pane.id === paneId)) {
        return;
      }
      const samePane = activePaneId === paneId && terminalMount;
      activePaneId = paneId;
      if (updateHistory && window.location.pathname !== panePath(paneId)) {
        window.history.pushState({ paneId }, "", panePath(paneId));
      }
      const pane = currentPane();
      updateShellState(pane || activePaneId ? "ready" : "empty");
      terminalIdentity.textContent = pane
        ? `${pane.title} · ${pane.cwd}`
        : `Pane ${paneId} · connecting`;
      terminalIdentity.title = terminalIdentity.textContent;
      renderTabs(false);
      renderActions();
      if (samePane) {
        if (pane && envelope) {
          document.title = `${pane.title} · ${envelope.window.name} · t1000`;
        }
        terminalMount.focus(updateHistory);
        if (focusTab) {
          focusTabButton(paneId);
        }
        return;
      }
      ({ mount: terminalMount, shell: xtermShell, host: terminalHost, overlay: terminalOverlay } = terminalViews.select(paneId));
      if (!focusTab) {
        terminalMount.focus(updateHistory);
      }
      if (pane && envelope) {
        document.title = `${pane.title} · ${envelope.window.name} · t1000`;
      }
      if (focusTab) {
        focusTabButton(paneId);
      }
    }

    function tabButton(pane, index) {
      const tab = node("div", "workspace-tab", "");
      tab.dataset.paneId = pane.id;
      tab.setAttribute("role", "presentation");
      if (pane.id === activePaneId) {
        tab.classList.add("workspace-tab-active");
      }
      const select = node("button", "workspace-tab-select", "");
      select.type = "button";
      select.setAttribute("role", "tab");
      select.setAttribute("aria-selected", pane.id === activePaneId ? "true" : "false");
      select.setAttribute("tabindex", pane.id === activePaneId || (!activePaneId && index === 0) ? "0" : "-1");
      select.dataset.paneId = pane.id;
      select.id = `termdeck-tab-${pane.id}`;
      select.setAttribute("aria-controls", "terminal-shell");
      select.append(node("span", "workspace-tab-title", pane.title));
      if (pane.pinned) {
        select.prepend(node("span", "workspace-tab-pin", "◆"));
        select.title = "Pinned tab";
      }
      const close = node("button", "workspace-tab-close", "×");
      close.type = "button";
      close.title = `Hide ${pane.title} without stopping it`;
      close.setAttribute("aria-label", `Hide ${pane.title}`);
      close.disabled = Boolean(pendingAction);
      tab.append(select, close);
      tab.title = `${pane.title}
${pane.cwd}`;
      select.addEventListener("click", () => switchPane(pane.id, true));
      select.addEventListener("dblclick", (event) => {
        event.preventDefault();
        runTabAction("rename", pane);
      });
      close.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        runTabAction("hide", pane);
      });
      tab.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        if (pane.id !== activePaneId) {
          switchPane(pane.id, true);
        }
        actionMenu.open = true;
        actionSummary.focus();
      });
      return tab;
    }

    function renderTabs(preserveFocus = true) {
      if (!envelope) {
        return;
      }
      windowTitle.textContent = envelope.window.name;
      workspaceStatus.textContent = `${envelope.window.panes.length} tabs`;
      const signature = JSON.stringify({
        activePaneId,
        pendingAction,
        windowName: envelope.window.name,
        panes: envelope.window.panes.map((pane) => ({
          id: pane.id,
          title: pane.title,
          cwd: pane.cwd,
          status: pane.status,
          pinned: pane.pinned,
          sessionId: pane.sessionId,
          controllerActive: pane.controllerActive,
        })),
      });
      if (signature === lastTabSignature) {
        return;
      }
      workspaceTools.updateTabs(envelope.window.panes, activePaneId);
      const focusedPaneId = preserveFocus && document.activeElement?.getAttribute("role") === "tab"
        ? document.activeElement.dataset.paneId
        : "";
      lastTabSignature = signature;
      tabbar.replaceChildren(...envelope.window.panes.map(tabButton));
      if (focusedPaneId && envelope.window.panes.some((pane) => pane.id === focusedPaneId)) {
        focusTabButton(focusedPaneId);
      }
    }

    function actionButton(action) {
      const button = node("button", action === "kill" ? "danger" : "");
      button.type = "button";
      button.dataset.action = action;
      button.addEventListener("click", () => runTabAction(button.dataset.action));
      return button;
    }

    function actionLink(label, href) {
      const element = link(label, href, "workspace-action-link");
      element.addEventListener("click", () => {
        actionMenu.open = false;
      });
      return element;
    }

    function renderActions() {
      const pane = currentPane();
      const signature = JSON.stringify({
        id: pane?.id || "",
        pendingAction,
        sessionId: pane?.sessionId || null,
        status: pane?.status || "",
        pinned: pane?.pinned || false,
      });
      if (signature === lastActionSignature) {
        return;
      }
      lastActionSignature = signature;
      if (!actionPanel.firstChild) {
        actionPanel.append(actionLink("All windows", appPath("/")), ...workspaceTools.menuItems(), node("hr", ""));
        actionPanel.append(...["rename", "pause", "pin", "hide", "kill"].map(actionButton));
      }
      const actions = [["Rename tab", "rename"],
        pane?.status === "paused" ? ["Resume", "resume"] : ["Pause", "pause"],
        pane?.pinned ? ["Unpin", "unpin"] : ["Pin", "pin"],
        ["Hide tab", "hide"], ["Kill process…", "kill"]];
      actionPanel.querySelectorAll("[data-action]").forEach((button, index) => {
        [button.textContent, button.dataset.action] = actions[index];
        button.disabled = Boolean(pendingAction) || !pane?.sessionId;
      });
    }

    async function runTabAction(action, targetPane = currentPane()) {
      const pane = targetPane;
      if (!pane || !pane.sessionId || pendingAction || actionDialog.open) {
        return;
      }
      const removedIndex = (action === "hide" || action === "kill") && envelope
        ? envelope.window.panes.findIndex((candidate) => candidate.id === pane.id)
        : -1;
      const request = { action, name: null, confirmation: null };
      if (action === "rename" || action === "kill") {
        const value = await askTabAction(action, pane);
        if (value === null || disposed) {
          return;
        }
        if (action === "rename") {
          request.name = value;
        } else {
          request.confirmation = pane.sessionId;
        }
      }
      actionMenu.open = false;
      pendingAction = action;
      newButton.disabled = true;
      actionNotice.hidden = true;
      renderTabs();
      renderActions();
      workspaceStatus.textContent = `${action}…`;
      const removesTab = action === "hide" || action === "kill";
      const removedActive = removesTab && pane.id === activePaneId;
      try {
        await mutateJson(
          appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/panes/${encodeURIComponent(pane.id)}/tab`),
          request,
          180000,
        );
        if (removedActive) {
          activePaneId = "";
          window.history.replaceState({}, "", appPath(`/${encodeURIComponent(routeSlug)}`));
        }
        await refreshWindow(true);
        if (removedActive) {
          const replacement = envelope?.window.panes[0];
          if (replacement) {
            switchPane(replacement.id, true, true);
          } else {
            newButton.focus();
          }
        } else if (removedIndex >= 0) {
          const remaining = orderedPaneIds();
          const focusId = remaining[Math.min(removedIndex, remaining.length - 1)];
          if (focusId) {
            focusTabButton(focusId);
          } else {
            newButton.focus();
          }
        }
      } catch (error) {
        actionNoticeText.textContent = `Could not ${action} “${pane.title}”. ${errorMessage(error)}`;
        actionNotice.hidden = false;
      } finally {
        pendingAction = "";
        newButton.disabled = false;
        renderTabs();
        renderActions();
      }
    }

    async function waitForPane(paneId) {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await refreshWindow(true);
        if (envelope?.window.panes.some((pane) => pane.id === paneId)) {
          return true;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 200));
      }
      return false;
    }

    async function createTab() {
      if (pendingAction) {
        return;
      }
      pendingAction = "create";
      newButton.disabled = true;
      workspaceStatus.textContent = "Creating tab…";
      renderActions();
      try {
        const result = await mutateJson(
          appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/tabs`),
          {},
          180000,
        );
        const created = result.payload?.payload?.tabs?.[0]?.id;
        if (typeof created !== "string" || created.length === 0) {
          throw new Error("The tab was created but its identity was not returned.");
        }
        const visible = await waitForPane(created);
        if (!visible) {
          throw new Error("The new tab did not appear in the live window snapshot.");
        }
        switchPane(created, true);
      } catch (error) {
        actionNoticeText.textContent = `Could not create tab. ${errorMessage(error)}`;
        actionNotice.hidden = false;
      } finally {
        pendingAction = "";
        newButton.disabled = false;
        renderActions();
      }
    }

    function chooseActivePane(windowData) {
      const locationPane = selectedFromLocation();
      const candidates = [locationPane, activePaneId, windowData.activePaneId, windowData.panes[0]?.id];
      return candidates.find((candidate) => candidate && windowData.panes.some((pane) => pane.id === candidate)) || "";
    }

    async function refreshWindow(immediate) {
      if (disposed) {
        return;
      }
      if (refreshInFlight) {
        if (immediate) {
          refreshRequested = true;
        }
        return;
      }
      refreshInFlight = true;
      try {
        root.dataset.termdeckState = "refreshing";
        root.setAttribute("aria-busy", "true");
        const { payload, response } = await fetchJson(
          appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}`),
          { headers: { Accept: "application/json" }, method: "GET" },
        );
        const next = normalizeWindowEnvelope(payload);
        if (next.window.slug !== routeSlug) {
          throw new Error("Window identity changed unexpectedly.");
        }
        if (envelope && next.generatedAt < envelope.generatedAt) {
          workspaceStatus.textContent = `${envelope.window.panes.length} tabs · reconnecting`;
          root.dataset.termdeckState = "stale";
          return;
        }
        envelope = next;
        terminalViews.prune(next.window.panes.map(pane => pane.id));
        const selected = chooseActivePane(next.window);
        renderTabs();
        if (selected && (!terminalMount || selected !== activePaneId)) {
          switchPane(selected, false);
        } else if (selected) {
          const pane = currentPane();
          terminalIdentity.textContent = pane
            ? `${pane.title} · ${pane.cwd}`
            : `Pane ${selected}`;
          terminalIdentity.title = terminalIdentity.textContent;
          renderActions();
          if (pane) {
            document.title = `${pane.title} · ${next.window.name} · t1000`;
          }
        } else {
          if (terminalMount) {
            terminalViews.clear();
            terminalMount = null;
          }
          activePaneId = "";
          terminalHost.replaceChildren();
          terminalIdentity.textContent = "No tab selected";
          terminalStatus.textContent = "No active terminal";
          terminalStatus.dataset.state = "offline";
          renderActions();
          updateShellState("empty");
        }
        workspaceStatus.textContent = pendingAction ? `${pendingAction}…` : response.headers.get("x-termdeck-stale") === "1"
          ? `${next.window.panes.length} tabs · reconnecting`
          : `${next.window.panes.length} tabs`;
        clearClientNotice();
        root.dataset.termdeckState = "ready";
      } catch (error) {
        workspaceStatus.textContent = "Refresh failed";
        updateShellState(terminalMount ? "degraded" : "error");
        root.dataset.termdeckState = "error";
        showClientNotice(`Window refresh failed. ${errorMessage(error)}`, "notice-error");
      } finally {
        refreshInFlight = false;
        root.setAttribute("aria-busy", "false");
        window.clearTimeout(refreshTimer);
        if (!disposed) {
          const queued = refreshRequested;
          refreshRequested = false;
          refreshTimer = window.setTimeout(
            () => refreshWindow(queued),
            queued ? 0 : document.hidden ? 30000 : 5000,
          );
        }
      }
    }

    newButton.addEventListener("click", () => {
      if (actionDialog.open || pendingAction) {
        return;
      }
      createTab();
    });
    function orderedPaneIds() {
      return envelope?.window.panes.map((pane) => pane.id) || [];
    }

    function cyclePane(direction) {
      const paneIds = orderedPaneIds();
      if (paneIds.length === 0) {
        return;
      }
      const current = Math.max(0, paneIds.indexOf(activePaneId));
      const selected = (current + direction + paneIds.length) % paneIds.length;
      switchPane(paneIds[selected], true);
    }

    window.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || actionDialog.open || workspaceTools.isOpen()) {
        return;
      }
      if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        newButton.click();
        return;
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && /^[1-9]$/.test(event.key)) {
        const paneId = orderedPaneIds()[Number(event.key) - 1];
        if (paneId) {
          event.preventDefault();
          switchPane(paneId, true);
        }
        return;
      }
      if (event.ctrlKey && !event.altKey && !event.metaKey && event.key === "PageUp") {
        event.preventDefault();
        cyclePane(-1);
        return;
      }
      if (event.ctrlKey && !event.altKey && !event.metaKey && event.key === "PageDown") {
        event.preventDefault();
        cyclePane(1);
        return;
      }
      if (event.key === "Escape" && actionMenu.open) {
        event.preventDefault();
        actionMenu.open = false;
        terminalMount?.focus();
      }
    });
    tabbar.addEventListener("wheel", (event) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || tabbar.scrollWidth <= tabbar.clientWidth) {
        return;
      }
      event.preventDefault();
      tabbar.scrollLeft += event.deltaY;
    }, { passive: false });
    tabbar.addEventListener("keydown", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[role=tab]") : null;
      if (!target || !tabbar.contains(target)) {
        return;
      }
      if (event.altKey || event.ctrlKey || event.metaKey) {
        return;
      }
      const paneIds = orderedPaneIds();
      if (paneIds.length === 0) {
        return;
      }
      const currentIndex = Math.max(0, paneIds.indexOf(target.dataset.paneId));
      let nextIndex = currentIndex;
      if (event.key === "ArrowRight") {
        nextIndex = (currentIndex + 1) % paneIds.length;
      } else if (event.key === "ArrowLeft") {
        nextIndex = (currentIndex - 1 + paneIds.length) % paneIds.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = paneIds.length - 1;
      } else {
        return;
      }
      event.preventDefault();
      switchPane(paneIds[nextIndex], true, true);
    });
    document.addEventListener("pointerdown", (event) => {
      if (actionMenu.open && !actionMenu.contains(event.target)) {
        actionMenu.open = false;
      }
    });
    window.addEventListener("popstate", () => {
      const paneId = selectedFromLocation();
      if (paneId) {
        switchPane(paneId, false);
      }
    });
    document.addEventListener("visibilitychange", () => {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => refreshWindow(false), 100);
    });
    window.addEventListener("beforeunload", () => {
      disposed = true;
      window.clearTimeout(refreshTimer);
      terminalViews.clear();
    }, { once: true });
    if (activePaneId) {
      switchPane(activePaneId, false);
    }
    refreshWindow(false);
  }

  if (page === "terminal" || page === "window") {
    startWorkspace(routePane);
  } else {
    startSnapshotRefresh();
  }

})();
