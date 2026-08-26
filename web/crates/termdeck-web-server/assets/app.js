"use strict";

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
  const endpoint = apiEndpoint(page, routeSlug);
  if (!endpoint) {
    return;
  }

  let requestInFlight = false;
  let refreshTimer = 0;
  let failureCount = 0;
  let requestController = null;

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

  function apiEndpoint(pageName, slug) {
    if (pageName === "home") {
      return "/_termdeck/api/v1/snapshot";
    }
    if (pageName === "window" && validSlug(slug)) {
      return `/_termdeck/api/v1/windows/${encodeURIComponent(slug)}`;
    }
    return "";
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
    element.setAttribute("role", "status");
    element.setAttribute("aria-live", "polite");
    return element;
  }

  function hostHeader(title, summary, identity) {
    const header = node("header");
    header.append(
      node("p", "eyebrow", "TermDeck read-only host mirror"),
      node("h1", "", title),
      node("p", "subtle", summary),
    );
    if (identity) {
      header.append(node("p", "identity", identity));
    }
    return header;
  }

  function footer(apiHref) {
    const footerElement = node("footer", "footer-row");
    footerElement.append(
      link("Versioned JSON", apiHref),
      node("p", "refresh-state", "Live refresh enabled · viewer-only browser session"),
    );
    return footerElement;
  }

  function renderHome(value, stale) {
    const host = normalizeHost(value);
    const fragment = document.createDocumentFragment();
    fragment.append(
      hostHeader(
        "Windows",
        `${host.windows.length} windows · ${host.totalPanes} visible panes · revision ${host.revision}`,
        "All browser sessions are viewers; this frontend has no mutation authority.",
      ),
    );
    if (stale) {
      fragment.append(
        notice(
          "The most recent host refresh failed. This bounded stale snapshot remains read-only.",
          "notice-stale",
        ),
      );
    }
    const grid = node("section", "grid");
    grid.setAttribute("aria-label", "TermDeck windows");
    for (const windowData of host.windows) {
      const running = windowData.panes.filter((pane) => pane.status === "running").length;
      const card = link("", windowData.path, "card");
      card.replaceChildren(
        node("span", "card-title", windowData.name),
        node("span", "card-meta", `${windowData.panes.length} panes · ${running} running`),
        node("span", "identity", `Window ${windowData.id}`),
        node("code", "", windowData.path),
      );
      grid.append(card);
    }
    if (host.windows.length === 0) {
      grid.append(node("div", "empty", "No remembered TermDeck windows are currently available."));
    }
    fragment.append(grid, footer("/_termdeck/api/v1/snapshot"));
    root.replaceChildren(fragment);
    document.title = "TermDeck";
  }

  function paneArticle(pane) {
    const article = node("article", "pane");
    const main = node("div", "pane-main");
    main.append(node("h2", "", pane.title), node("p", "path", pane.cwd));
    const identity = pane.sessionId
      ? `Pane ${pane.id} · session ${pane.sessionId}`
      : `Pane ${pane.id}`;
    main.append(node("p", "identity", identity));

    const aside = node("div", "pane-aside");
    aside.append(
      node("span", `status status-${pane.status}`, pane.status),
      node("span", "mode", "Viewer · read-only"),
    );
    if (pane.controllerActive) {
      aside.append(node("span", "controller-active", "Controller active"));
    }
    article.append(main, aside);
    return article;
  }

  function renderWindow(value, stale) {
    const envelope = normalizeWindowEnvelope(value);
    const windowData = envelope.window;
    if (routeSlug && windowData.slug !== routeSlug) {
      throw new Error("Window route identity changed unexpectedly.");
    }
    const fragment = document.createDocumentFragment();
    const navigation = node("nav");
    navigation.append(link("← All windows", "/"));
    fragment.append(
      navigation,
      hostHeader(
        windowData.name,
        `${windowData.panes.length} visible panes · revision ${envelope.revision}`,
        `Window ${windowData.id} · ${windowData.path}`,
      ),
    );
    if (stale) {
      fragment.append(
        notice(
          "Live host observation is temporarily degraded. No terminal state has been changed.",
          "notice-stale",
        ),
      );
    }
    const panes = node("section", "panes");
    panes.setAttribute("aria-label", "Terminal panes");
    for (const pane of windowData.panes) {
      panes.append(paneArticle(pane));
    }
    if (windowData.panes.length === 0) {
      panes.append(node("div", "empty", "This remembered window currently has no visible panes."));
    }
    fragment.append(
      panes,
      footer(`/_termdeck/api/v1/windows/${encodeURIComponent(windowData.slug)}`),
    );
    root.replaceChildren(fragment);
    document.title = `${windowData.name} · TermDeck`;
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
  }

  function clearClientNotice() {
    const element = currentClientNotice();
    if (element) {
      element.remove();
    }
  }

  function errorMessage(error) {
    if (error instanceof Error && error.message) {
      return error.message.slice(0, 240);
    }
    return "Host refresh failed.";
  }

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
    requestController = new AbortController();
    const timeoutId = window.setTimeout(() => requestController.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await window.fetch(endpoint, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        method: "GET",
        signal: requestController.signal,
      });
      if (!response.ok) {
        throw new Error(`Host API returned HTTP ${response.status}.`);
      }
      const payload = await response.json();
      const stale = response.headers.get("x-termdeck-stale") === "1";
      if (page === "home") {
        renderHome(payload, stale);
      } else {
        renderWindow(payload, stale);
      }
      failureCount = 0;
      if (!stale) {
        clearClientNotice();
      }
    } catch (error) {
      failureCount = Math.min(failureCount + 1, 6);
      showClientNotice(
        `Live refresh unavailable. ${errorMessage(error)} Retrying without enabling control.`,
        "notice-error",
      );
    } finally {
      window.clearTimeout(timeoutId);
      requestController = null;
      requestInFlight = false;
      scheduleRefresh();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      window.clearTimeout(refreshTimer);
      refresh();
    }
  });
  window.addEventListener("online", () => {
    failureCount = 0;
    window.clearTimeout(refreshTimer);
    refresh();
  });
  window.addEventListener("beforeunload", () => {
    window.clearTimeout(refreshTimer);
    if (requestController) {
      requestController.abort();
    }
  });

  refresh();
})();
