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
  const MAX_TERMINAL_TEXT_CHARS = 2 * 1024 * 1024;
  const REFRESH_VISIBLE_MS = 2000;
  const REFRESH_HIDDEN_MS = 15000;
  const TERMINAL_REFRESH_VISIBLE_MS = 1000;
  const TERMINAL_REFRESH_HIDDEN_MS = 5000;
  const HEARTBEAT_MS = 10000;
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
      node("p", "eyebrow", "TermDeck web host"),
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
      node("p", "refresh-state", "Live refresh enabled · authenticated browser session"),
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
        "Open any window to work with its live terminals.",
      ),
    );
    if (stale) {
      fragment.append(
        notice("The most recent host refresh failed. Showing a bounded stale snapshot.", "notice-stale"),
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

  function paneArticle(windowSlug, pane) {
    const path = `/${encodeURIComponent(windowSlug)}/t/${encodeURIComponent(pane.id)}`;
    const article = link("", path, "pane pane-link");
    const main = node("div", "pane-main");
    main.append(node("h2", "", pane.title), node("p", "path", pane.cwd));
    const identity = pane.sessionId
      ? `Pane ${pane.id} · session ${pane.sessionId}`
      : `Pane ${pane.id}`;
    main.append(node("p", "identity", identity));
    const aside = node("div", "pane-aside");
    aside.append(
      node("span", `status status-${pane.status}`, pane.status),
      node("span", "mode", "Open terminal"),
    );
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
        notice("Live host observation is temporarily degraded.", "notice-stale"),
      );
    }
    const panes = node("section", "panes");
    panes.setAttribute("aria-label", "Terminal panes");
    for (const pane of windowData.panes) {
      panes.append(paneArticle(windowData.slug, pane));
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
      if (response.redirected && response.url.includes("/_termdeck/login")) {
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

  function startSnapshotRefresh() {
    const endpoint = apiEndpoint(page, routeSlug);
    if (!endpoint) {
      return;
    }
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
      try {
        const { payload, response } = await fetchJson(endpoint, {
          headers: { Accept: "application/json" },
          method: "GET",
        });
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
        showClientNotice(`Live refresh unavailable. ${errorMessage(error)} Retrying.`, "notice-error");
      } finally {
        requestInFlight = false;
        scheduleRefresh();
      }
    }

    document.addEventListener("visibilitychange", scheduleRefresh);
    refresh();
  }

  function mountTerminal(windowSlug, paneId, host, status, overlay) {
    const library = window.TermDeckTerminal;
    if (!host || !status || !overlay || !library || typeof library.createTerminal !== "function") {
      throw new Error("The interactive terminal assets did not load.");
    }

    const MAX_PENDING_BYTES = 1024 * 1024;
    const MAX_FRAME_BYTES = 32 * 1024;
    const MAX_RECONNECT_MS = 5000;
    const MIN_COLUMNS = 2;
    const MAX_COLUMNS = 1000;
    const MIN_ROWS = 2;
    const MAX_ROWS = 500;
    const terminalApi = library.createTerminal(host, { scrollback: 20000 });
    Object.defineProperty(host, "termdeckTerminal", {
      configurable: true,
      value: terminalApi,
      writable: false,
    });
    let socket = null;
    let ready = false;
    let mine = false;
    let nextSequence = 1;
    let reconnectAttempt = 0;
    let reconnectTimer = 0;
    let resizeTimer = 0;
    let webglTimer = 0;
    let helloTimer = 0;
    let heartbeatTimer = 0;
    let lastPongAt = Date.now();
    let queuedBytes = 0;
    let queue = [];
    let inFlight = null;
    let disposed = false;
    let outputGeneration = 0;
    let outputChain = Promise.resolve();
    let helloGeometry = null;
    let initialResyncComplete = false;
    let resyncPending = false;

    function setStatus(message, kind) {
      status.textContent = message;
      status.dataset.state = kind || "";
      status.title = message;
      status.setAttribute("aria-label", message);
    }

    function setOverlay(message) {
      overlay.hidden = !message;
      overlay.textContent = message || "";
    }

    function boundedGeometry(value) {
      const columns = Number.isInteger(value?.cols) ? value.cols : MIN_COLUMNS;
      const rows = Number.isInteger(value?.rows) ? value.rows : MIN_ROWS;
      return {
        cols: Math.max(MIN_COLUMNS, Math.min(MAX_COLUMNS, columns)),
        rows: Math.max(MIN_ROWS, Math.min(MAX_ROWS, rows)),
      };
    }

    function fitGeometry() {
      return boundedGeometry(terminalApi.fit());
    }

    function streamUrl() {
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      return `${scheme}//${window.location.host}/_termdeck/ws/v1/windows/${encodeURIComponent(windowSlug)}/panes/${encodeURIComponent(paneId)}`;
    }

    function socketOpen() {
      return socket && socket.readyState === WebSocket.OPEN;
    }

    function sendJson(value) {
      if (socketOpen()) {
        socket.send(JSON.stringify(value));
      }
    }

    function applyControl(value) {
      if (!isRecord(value) || value.schema !== "termdeck.terminal-control.v1") {
        throw new Error("Invalid terminal control state.");
      }
      mine = value.mine === true;
      if (value.next_sequence !== null && value.next_sequence !== undefined) {
        nextSequence = nonNegativeInteger(value.next_sequence, "terminal input sequence");
      } else if (!mine) {
        nextSequence = 1;
      }
      if (value.indeterminate === true) {
        setStatus("Input state uncertain · reconnect required", "error");
      } else if (mine) {
        setStatus("Live", "live");
      } else {
        setStatus("Live", "viewer");
      }
    }

    function binaryFrame(sequence, bytes) {
      const frame = new Uint8Array(9 + bytes.length);
      frame[0] = 1;
      const view = new DataView(frame.buffer);
      view.setBigUint64(1, BigInt(sequence), false);
      frame.set(bytes, 9);
      return frame;
    }

    function discardAmbiguousInput() {
      if (inFlight) {
        queuedBytes = Math.max(0, queuedBytes - inFlight.bytes.length);
        inFlight = null;
        setOverlay("The connection changed while input was in flight. That input was not replayed automatically.");
      }
    }

    function sendNext() {
      if (!ready || !socketOpen() || inFlight || queue.length === 0) {
        return;
      }
      inFlight = queue.shift();
      inFlight.sequence = nextSequence;
      socket.send(binaryFrame(inFlight.sequence, inFlight.bytes));
    }

    function enqueue(bytes) {
      if (!(bytes instanceof Uint8Array) || bytes.length === 0 || disposed) {
        return;
      }
      for (let offset = 0; offset < bytes.length; offset += MAX_FRAME_BYTES) {
        const chunk = bytes.slice(offset, Math.min(bytes.length, offset + MAX_FRAME_BYTES));
        if (queuedBytes + chunk.length > MAX_PENDING_BYTES) {
          setOverlay("Terminal input queue is full. Wait for the remote terminal to catch up.");
          return;
        }
        queue.push({ bytes: chunk, sequence: null });
        queuedBytes += chunk.length;
      }
      sendNext();
    }

    function queueTerminalReset() {
      outputGeneration += 1;
      const generation = outputGeneration;
      outputChain = outputChain.then(() => {
        if (!disposed && generation === outputGeneration) {
          terminalApi.reset();
        }
      });
      return generation;
    }

    function queueTerminalWrite(value) {
      const generation = outputGeneration;
      const bytes = value instanceof Uint8Array ? value.slice() : new Uint8Array(value);
      outputChain = outputChain.then(
        () => new Promise((resolve) => {
          if (disposed || generation !== outputGeneration) {
            resolve();
            return;
          }
          terminalApi.write(bytes, () => {
            resolve();
          });
        }),
      );
      return outputChain;
    }

    function afterTerminalOutput(generation, callback) {
      outputChain.then(() => {
        if (!disposed && generation === outputGeneration) {
          callback();
        }
      });
    }

    function replayGeometryImproved(current, initial) {
      if (!current || !initial || current.rows <= initial.rows) {
        return false;
      }
      const currentCells = current.cols * current.rows;
      const initialCells = initial.cols * initial.rows;
      return currentCells > initialCells;
    }

    function requestInitialResync(dimensions) {
      if (resyncPending || initialResyncComplete || !socketOpen()) {
        return false;
      }
      resyncPending = true;
      ready = false;
      setStatus("Synchronizing…", "reconnecting");
      sendJson({ type: "resync", cols: dimensions.cols, rows: dimensions.rows });
      window.clearTimeout(helloTimer);
      helloTimer = window.setTimeout(() => {
        if (resyncPending && socketOpen()) {
          socket.close(4006, "terminal resync timeout");
        }
      }, 5000);
      return true;
    }

    function finishReady(event, resynced = false) {
      applyControl(event.control);
      reconnectAttempt = 0;
      lastPongAt = Date.now();
      window.clearTimeout(helloTimer);
      setOverlay("");
      if (resynced) {
        initialResyncComplete = true;
        resyncPending = false;
      } else if (!initialResyncComplete && helloGeometry) {
        const dimensions = fitGeometry();
        if (
          replayGeometryImproved(dimensions, helloGeometry)
          && requestInitialResync(dimensions)
        ) {
          return;
        }
      }
      ready = true;
      terminalApi.focus();
      sendJson({ type: "focus" });
      sendNext();
      window.clearTimeout(webglTimer);
      webglTimer = window.setTimeout(() => terminalApi.activateWebgl(), 2000);
    }

    function applyServerEvent(event) {
      if (!isRecord(event) || typeof event.type !== "string") {
        throw new Error("Invalid terminal stream event.");
      }
      if (event.type === "reset") {
        queueTerminalReset();
        return;
      }
      if (event.type === "ready") {
        const generation = outputGeneration;
        afterTerminalOutput(generation, () => finishReady(event));
        return;
      }
      if (event.type === "resynced") {
        const generation = outputGeneration;
        afterTerminalOutput(generation, () => finishReady(event, true));
        return;
      }
      if (event.type === "control") {
        applyControl(event.control);
        return;
      }
      if (event.type === "ack") {
        const sequence = nonNegativeInteger(event.sequence, "terminal acknowledgement");
        if (!inFlight || inFlight.sequence !== sequence) {
          throw new Error("Terminal acknowledgement was out of order.");
        }
        queuedBytes = Math.max(0, queuedBytes - inFlight.bytes.length);
        inFlight = null;
        applyControl(event.control);
        sendNext();
        return;
      }
      if (event.type === "pong") {
        lastPongAt = Date.now();
        return;
      }
      if (event.type === "error") {
        const message = typeof event.message === "string"
          ? event.message.slice(0, 400)
          : "Terminal stream failed.";
        setOverlay(message);
        if (event.recoverable !== true && socket) {
          socket.close(4003, "terminal protocol error");
        }
        return;
      }
      throw new Error("Unknown terminal stream event.");
    }

    function scheduleReconnect() {
      if (disposed) {
        return;
      }
      window.clearTimeout(reconnectTimer);
      const delay = Math.min(MAX_RECONNECT_MS, 250 * 2 ** Math.min(reconnectAttempt, 5));
      reconnectAttempt += 1;
      setStatus("Reconnecting…", "reconnecting");
      reconnectTimer = window.setTimeout(connect, delay);
    }

    function sendHelloWhenReady(candidate) {
      if (
        disposed
        || candidate !== socket
        || candidate.readyState !== WebSocket.OPEN
      ) {
        return;
      }
      const dimensions = fitGeometry();
      helloGeometry = dimensions;
      candidate.send(JSON.stringify({ type: "hello", cols: dimensions.cols, rows: dimensions.rows }));
      helloTimer = window.setTimeout(() => {
        if (candidate === socket && !ready) {
          candidate.close(4001, "terminal hello timeout");
        }
      }, 5000);
    }

    function fitAndResize() {
      if (disposed) {
        return;
      }
      const dimensions = fitGeometry();
      if (ready && socketOpen()) {
        if (
          !initialResyncComplete
          && helloGeometry
          && replayGeometryImproved(dimensions, helloGeometry)
          && requestInitialResync(dimensions)
        ) {
          return;
        }
        sendJson({ type: "resize", cols: dimensions.cols, rows: dimensions.rows });
      }
    }

    function connect() {
      if (disposed) {
        return;
      }
      if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
        return;
      }
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(helloTimer);
      ready = false;
      mine = false;
      helloGeometry = null;
      initialResyncComplete = false;
      resyncPending = false;
      setStatus("Connecting…", "connecting");
      const candidate = new WebSocket(streamUrl(), "termdeck.terminal.v1");
      candidate.binaryType = "arraybuffer";
      socket = candidate;
      candidate.addEventListener("open", () => {
        if (candidate !== socket) {
          candidate.close();
          return;
        }
        sendHelloWhenReady(candidate);
      });
      candidate.addEventListener("message", (message) => {
        if (candidate !== socket) {
          return;
        }
        try {
          if (typeof message.data === "string") {
            applyServerEvent(JSON.parse(message.data));
          } else if (message.data instanceof ArrayBuffer) {
            queueTerminalWrite(new Uint8Array(message.data));
          } else {
            throw new Error("Unsupported terminal stream frame.");
          }
        } catch (error) {
          setOverlay(errorMessage(error));
          candidate.close(4004, "invalid terminal frame");
        }
      });
      candidate.addEventListener("close", () => {
        if (candidate !== socket) {
          return;
        }
        socket = null;
        ready = false;
        mine = false;
        window.clearTimeout(helloTimer);
          discardAmbiguousInput();
        scheduleReconnect();
      });
      candidate.addEventListener("error", () => {
        setStatus("Terminal connection failed", "error");
      });
    }

    terminalApi.onData(enqueue);
    terminalApi.onResize((value) => {
      if (ready && socketOpen()) {
        const { cols, rows } = boundedGeometry(value);
        sendJson({ type: "resize", cols, rows });
      }
    });
    host.addEventListener("focusin", () => sendJson({ type: "focus" }));
    host.addEventListener("pointerdown", () => {
      terminalApi.focus();
      sendJson({ type: "focus" });
    });
    const observer = new ResizeObserver(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(fitAndResize, 60);
    });
    observer.observe(host);
    const reconnectNow = () => {
      reconnectAttempt = 0;
      if (!socketOpen()) {
        connect();
      }
    };
    window.addEventListener("online", reconnectNow);
    heartbeatTimer = window.setInterval(() => {
      if (!ready || !socketOpen()) {
        return;
      }
      if (Date.now() - lastPongAt > 45000) {
        socket.close(4002, "terminal heartbeat timeout");
        return;
      }
      sendJson({ type: "ping" });
    }, 15000);
    connect();

    return {
      dispose() {
        if (disposed) {
          return;
        }
        disposed = true;
        observer.disconnect();
        window.clearTimeout(reconnectTimer);
        window.clearTimeout(resizeTimer);
        window.clearTimeout(webglTimer);
        window.clearTimeout(helloTimer);
          window.clearInterval(heartbeatTimer);
        window.removeEventListener("online", reconnectNow);
        queue = [];
        queuedBytes = 0;
        inFlight = null;
        if (socket) {
          socket.close(1000, "terminal switched");
          socket = null;
        }
        try {
          delete host.termdeckTerminal;
        } catch (_ignored) {
          // The host will be discarded with the workspace during navigation.
        }
        terminalApi.dispose();
      },
      focus() {
        terminalApi.focus();
        sendJson({ type: "focus" });
      },
    };
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
    const homeLink = link("T1000", "/", "workspace-home");
    homeLink.title = "All TermDeck windows";
    const newButton = node("button", "workspace-new-tab", "+");
    newButton.type = "button";
    newButton.title = "New tab";
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
    rightTools.append(workspaceStatus, terminalStatus, actionMenu);
    topbar.append(leftTools, titleGroup, rightTools);

    const tabbar = node("div", "workspace-tabbar");
    tabbar.setAttribute("role", "tablist");
    tabbar.setAttribute("aria-label", "TermDeck tabs");
    const shell = node("section", "workspace-shell");
    const terminalIdentity = node("span", "workspace-terminal-identity", "No tab selected");
    const xtermShell = node("div", "xterm-shell");
    xtermShell.id = "terminal-shell";
    const terminalHost = node("div", "xterm-host");
    terminalHost.id = "terminal-xterm";
    terminalHost.setAttribute("data-termdeck-terminal", "1");
    const terminalOverlay = node("div", "terminal-overlay");
    terminalOverlay.id = "terminal-overlay";
    terminalOverlay.hidden = true;
    xtermShell.append(terminalHost, terminalOverlay);
    shell.append(terminalIdentity, xtermShell);

    const dialog = document.createElement("dialog");
    dialog.className = "workspace-dialog";
    const newForm = document.createElement("form");
    newForm.method = "dialog";
    const dialogTitle = node("h2", "", "New tab");
    const cwdInput = document.createElement("input");
    cwdInput.name = "cwd";
    cwdInput.required = true;
    cwdInput.autocomplete = "off";
    const nameInput = document.createElement("input");
    nameInput.name = "name";
    nameInput.autocomplete = "off";
    const commandInput = document.createElement("input");
    commandInput.name = "command";
    commandInput.autocomplete = "off";
    const pinnedInput = document.createElement("input");
    pinnedInput.type = "checkbox";
    pinnedInput.name = "pinned";
    const dialogStatus = node("p", "workspace-dialog-status", "");
    const cancelButton = node("button", "secondary", "Cancel");
    cancelButton.type = "button";
    const createButton = node("button", "", "Create tab");
    createButton.type = "submit";
    const formActions = node("div", "workspace-dialog-actions");
    formActions.append(cancelButton, createButton);
    function labeled(labelText, input) {
      const label = node("label", "workspace-field");
      label.append(node("span", "", labelText), input);
      return label;
    }
    const pinLabel = node("label", "workspace-check");
    pinLabel.append(pinnedInput, node("span", "", "Pin this tab"));
    newForm.append(
      dialogTitle,
      labeled("Working directory", cwdInput),
      labeled("Tab name (optional)", nameInput),
      labeled("Command (optional)", commandInput),
      pinLabel,
      dialogStatus,
      formActions,
    );
    dialog.append(newForm);

    root.replaceChildren(topbar, tabbar, shell, dialog);

    let envelope = null;
    let activePaneId = initialPaneId || "";
    let terminalMount = null;
    let refreshTimer = 0;
    let refreshInFlight = false;
    let disposed = false;

    function currentPane() {
      return envelope?.window.panes.find((pane) => pane.id === activePaneId) || null;
    }

    function panePath(paneId) {
      return `/${encodeURIComponent(routeSlug)}/t/${encodeURIComponent(paneId)}`;
    }

    function selectedFromLocation() {
      const prefix = `/${routeSlug}/t/`;
      if (!window.location.pathname.startsWith(prefix)) {
        return "";
      }
      try {
        return decodeURIComponent(window.location.pathname.slice(prefix.length));
      } catch (_error) {
        return "";
      }
    }

    function switchPane(paneId, updateHistory) {
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
      terminalIdentity.textContent = pane
        ? `${pane.title} · ${pane.cwd}`
        : `Pane ${paneId} · connecting`;
      renderTabs();
      renderActions();
      if (samePane) {
        if (pane && envelope) {
          document.title = `${pane.title} · ${envelope.window.name} · TermDeck`;
        }
        terminalMount.focus();
        return;
      }
      if (terminalMount) {
        terminalMount.dispose();
        terminalMount = null;
      }
      terminalHost.replaceChildren();
      terminalMount = mountTerminal(routeSlug, paneId, terminalHost, terminalStatus, terminalOverlay);
      if (pane && envelope) {
        document.title = `${pane.title} · ${envelope.window.name} · TermDeck`;
      }
    }

    function tabButton(pane) {
      const tab = node("div", "workspace-tab", "");
      tab.dataset.paneId = pane.id;
      if (pane.id === activePaneId) {
        tab.classList.add("workspace-tab-active");
      }
      const select = node("button", "workspace-tab-select", "");
      select.type = "button";
      select.setAttribute("role", "tab");
      select.setAttribute("aria-selected", pane.id === activePaneId ? "true" : "false");
      select.append(node("span", "workspace-tab-title", pane.title));
      const close = node("button", "workspace-tab-close", "×");
      close.type = "button";
      close.title = `Hide ${pane.title} without stopping it`;
      close.setAttribute("aria-label", `Hide ${pane.title}`);
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

    function renderTabs() {
      if (!envelope) {
        return;
      }
      tabbar.replaceChildren(...envelope.window.panes.map(tabButton));
      windowTitle.textContent = envelope.window.name;
      workspaceStatus.textContent = `${envelope.window.panes.length} tabs`;
    }

    function actionButton(label, action, destructive) {
      const button = node("button", destructive ? "danger" : "", label);
      button.type = "button";
      button.addEventListener("click", () => runTabAction(action));
      return button;
    }

    function renderActions() {
      actionPanel.replaceChildren();
      const pane = currentPane();
      if (!pane || !pane.sessionId) {
        actionPanel.append(node("p", "subtle", "This tab has no mutable session identity."));
        return;
      }
      actionPanel.append(
        actionButton("Rename tab", "rename", false),
        actionButton(pane.status === "paused" ? "Resume" : "Pause", pane.status === "paused" ? "resume" : "pause", false),
        actionButton("Pin", "pin", false),
        actionButton("Unpin", "unpin", false),
        actionButton("Hide tab", "hide", false),
        actionButton("Kill process…", "kill", true),
      );
    }

    async function runTabAction(action, targetPane = currentPane()) {
      const pane = targetPane;
      if (!pane || !pane.sessionId) {
        return;
      }
      const request = { action, name: null, confirmation: null };
      if (action === "rename") {
        const name = window.prompt("New tab name", pane.title);
        if (name === null) {
          return;
        }
        request.name = name;
      }
      if (action === "kill") {
        const accepted = window.confirm(`Kill ${pane.title}? The terminal process will be stopped.`);
        if (!accepted) {
          return;
        }
        request.confirmation = pane.sessionId;
      }
      actionMenu.open = false;
      workspaceStatus.textContent = `${action}…`;
      const removesTab = action === "hide" || action === "kill";
      const removedActive = removesTab && pane.id === activePaneId;
      try {
        await mutateJson(
          `/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/panes/${encodeURIComponent(pane.id)}/tab`,
          request,
          180000,
        );
        if (removedActive) {
          activePaneId = "";
          window.history.replaceState({}, "", `/${encodeURIComponent(routeSlug)}`);
        }
        await refreshWindow(true);
        if (removedActive) {
          const replacement = envelope?.window.panes[0];
          if (replacement) {
            switchPane(replacement.id, true);
          }
        }
      } catch (error) {
        terminalOverlay.hidden = false;
        terminalOverlay.textContent = `Tab action failed. ${errorMessage(error)}`;
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
      dialogStatus.textContent = "Creating…";
      createButton.disabled = true;
      const payload = {
        cwd: cwdInput.value,
        name: nameInput.value.trim() || null,
        command: commandInput.value.trim() || null,
        pinned: pinnedInput.checked,
      };
      try {
        const result = await mutateJson(
          `/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/tabs`,
          payload,
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
        dialog.close();
        newForm.reset();
        switchPane(created, true);
      } catch (error) {
        dialogStatus.textContent = errorMessage(error);
      } finally {
        createButton.disabled = false;
      }
    }

    function chooseActivePane(windowData) {
      const locationPane = selectedFromLocation();
      const candidates = [locationPane, activePaneId, windowData.activePaneId, windowData.panes[0]?.id];
      return candidates.find((candidate) => candidate && windowData.panes.some((pane) => pane.id === candidate)) || "";
    }

    async function refreshWindow(immediate) {
      if (refreshInFlight || disposed) {
        return;
      }
      refreshInFlight = true;
      try {
        const { payload, response } = await fetchJson(
          `/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}`,
          { headers: { Accept: "application/json" }, method: "GET" },
        );
        const next = normalizeWindowEnvelope(payload);
        if (next.window.slug !== routeSlug) {
          throw new Error("Window identity changed unexpectedly.");
        }
        envelope = next;
        const selected = chooseActivePane(next.window);
        renderTabs();
        if (selected && (!terminalMount || selected !== activePaneId)) {
          switchPane(selected, false);
        } else if (selected) {
          const pane = currentPane();
          terminalIdentity.textContent = pane
            ? `${pane.title} · ${pane.cwd}`
            : `Pane ${selected}`;
          renderActions();
          if (pane) {
            document.title = `${pane.title} · ${next.window.name} · TermDeck`;
          }
        } else {
          if (terminalMount) {
            terminalMount.dispose();
            terminalMount = null;
          }
          activePaneId = "";
          terminalHost.replaceChildren();
          terminalIdentity.textContent = "No tab selected";
          renderActions();
        }
        workspaceStatus.textContent = response.headers.get("x-termdeck-stale") === "1"
          ? `${next.window.panes.length} tabs · reconnecting`
          : `${next.window.panes.length} tabs`;
        clearClientNotice();
      } catch (error) {
        workspaceStatus.textContent = "Refresh failed";
        showClientNotice(`Window refresh failed. ${errorMessage(error)}`, "notice-error");
      } finally {
        refreshInFlight = false;
        if (!immediate && !disposed) {
          window.clearTimeout(refreshTimer);
          refreshTimer = window.setTimeout(() => refreshWindow(false), document.hidden ? 30000 : 5000);
        }
      }
    }

    newButton.addEventListener("click", () => {
      const pane = currentPane();
      cwdInput.value = pane?.cwd || "/";
      dialogStatus.textContent = "";
      dialog.showModal();
      window.setTimeout(() => cwdInput.focus(), 0);
    });
    cancelButton.addEventListener("click", () => dialog.close());
    newForm.addEventListener("submit", (event) => {
      event.preventDefault();
      createTab();
    });
    window.addEventListener("keydown", (event) => {
      if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        newButton.click();
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
      if (terminalMount) {
        terminalMount.dispose();
      }
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
