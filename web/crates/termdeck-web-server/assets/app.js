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

  function apiEndpoint(pageName, slug) {
    if (pageName === "home") {
      return appPath("/_termdeck/api/v1/snapshot");
    }
    if (pageName === "window" && validSlug(slug)) {
      return appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(slug)}`);
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
    const urgent = kind === "notice-error";
    element.setAttribute("role", urgent ? "alert" : "status");
    element.setAttribute("aria-live", urgent ? "assertive" : "polite");
    return element;
  }

  function hostHeader(title, summary, identity) {
    const header = node("header", "host-header");
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

  function footer(apiHref, stale) {
    const footerElement = node("footer", "footer-row");
    footerElement.append(
      link("Versioned JSON", apiHref, "footer-link"),
      node(
        "p",
        "refresh-state",
        stale
          ? "Showing saved windows · reconnecting"
          : "Live refresh enabled · authenticated browser session",
      ),
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
      const card = link("", appPath(windowData.path), "card");
      card.replaceChildren(
        node("span", "card-title", windowData.name),
        node("span", "card-meta", `${windowData.panes.length} panes · ${running} running`),
        node("span", "identity", `Window ${windowData.id}`),
        node("code", "", appPath(windowData.path)),
      );
      grid.append(card);
    }
    if (host.windows.length === 0) {
      grid.append(node("div", "empty", "No remembered TermDeck windows are currently available."));
    }
    fragment.append(grid, footer(appPath("/_termdeck/api/v1/snapshot"), stale));
    root.replaceChildren(fragment);
    root.dataset.termdeckState = stale ? "stale" : "ready";
    root.setAttribute("aria-busy", "false");
    document.title = "TermDeck";
  }

  function paneArticle(windowSlug, pane) {
    const path = appPath(`/${encodeURIComponent(windowSlug)}/t/${encodeURIComponent(pane.id)}`);
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
    const navigation = node("nav", "page-nav");
    navigation.append(link("← All windows", appPath("/"), "back-link"));
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
      footer(appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(windowData.slug)}`), stale),
    );
    root.replaceChildren(fragment);
    root.dataset.termdeckState = stale ? "stale" : "ready";
    root.setAttribute("aria-busy", "false");
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
      root.dataset.termdeckState = "refreshing";
      root.setAttribute("aria-busy", "true");
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

  function mountTerminal(windowSlug, paneId, host, status, overlay) {
    const library = window.TermDeckTerminal;
    if (!host || !status || !overlay || !library || typeof library.createTerminal !== "function") {
      throw new Error("The interactive terminal assets did not load.");
    }

    const MAX_PENDING_BYTES = 1024 * 1024;
    const MAX_FRAME_BYTES = 32 * 1024;
    const MAX_IN_FLIGHT_FRAMES = 64;
    const INPUT_BATCH_MS = 3;
    const MAX_RECONNECT_MS = 5000;
    const MAX_SOCKET_BUFFERED_BYTES = 512 * 1024;
    const SOCKET_DRAIN_MS = 8;
    const MAX_OUTPUT_BATCH_BYTES = 256 * 1024;
    const MAX_OUTPUT_QUEUE_BYTES = 16 * 1024 * 1024;
    const OUTPUT_HIDDEN_DELAY_MS = 16;
    const STABLE_CONNECTION_MS = 10000;
    const HEARTBEAT_INTERVAL_MS = 15000;
    const HEARTBEAT_VISIBLE_TIMEOUT_MS = 45000;
    const HEARTBEAT_HIDDEN_TIMEOUT_MS = 5 * 60 * 1000;
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
    let inputBatchTimer = 0;
    let stableTimer = 0;
    let socketDrainTimer = 0;
    let outputFlushTimer = 0;
    let outputAnimationFrame = 0;
    let socketGeneration = 0;
    let lastPongAt = Date.now();
    let networkOnline = window.navigator.onLine !== false;
    let queuedBytes = 0;
    let queue = [];
    let inFlight = new Map();
    let batchChunks = [];
    let batchBytes = 0;
    let disposed = false;
    let outputGeneration = 0;
    let outputChain = Promise.resolve();
    let outputChunks = [];
    let outputBytes = 0;
    let canonicalGeometry = null;

    function setStatus(message, kind) {
      status.textContent = message;
      status.dataset.state = kind || "";
      status.title = message;
      status.setAttribute("aria-label", message);
      document.body.dataset.termdeckConnection = kind || "";
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

    function requiredGeometry(value) {
      if (
        !isRecord(value)
        || !Number.isInteger(value.cols)
        || !Number.isInteger(value.rows)
        || value.cols < MIN_COLUMNS
        || value.cols > MAX_COLUMNS
        || value.rows < MIN_ROWS
        || value.rows > MAX_ROWS
      ) {
        throw new Error("Invalid canonical terminal geometry.");
      }
      return { cols: value.cols, rows: value.rows };
    }

    function fitViewport() {
      if (canonicalGeometry) {
        return terminalApi.fitGrid(canonicalGeometry.cols, canonicalGeometry.rows);
      }
      return terminalApi.fit();
    }

    function streamUrl() {
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      return `${scheme}//${window.location.host}${appPath(`/_termdeck/ws/v1/windows/${encodeURIComponent(windowSlug)}/panes/${encodeURIComponent(paneId)}`)}`;
    }

    function socketOpen(candidate = socket) {
      return candidate && candidate.readyState === WebSocket.OPEN;
    }

    function scheduleSocketDrain() {
      if (disposed || socketDrainTimer) {
        return;
      }
      socketDrainTimer = window.setTimeout(() => {
        socketDrainTimer = 0;
        sendAvailable();
      }, SOCKET_DRAIN_MS);
    }

    function sendJson(value) {
      if (!socketOpen()) {
        return false;
      }
      if (socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
        scheduleSocketDrain();
        return false;
      }
      socket.send(JSON.stringify(value));
      return true;
    }

    function remoteNextSequence(value) {
      if (value.next_sequence === null || value.next_sequence === undefined) {
        return null;
      }
      return nonNegativeInteger(value.next_sequence, "terminal input sequence");
    }

    function applyControl(value, synchronizeSequence = false) {
      if (!isRecord(value) || value.schema !== "termdeck.terminal-control.v1") {
        throw new Error("Invalid terminal control state.");
      }
      const previouslyMine = mine;
      mine = value.mine === true;
      const remoteNext = remoteNextSequence(value);
      if (remoteNext !== null) {
        if (synchronizeSequence || inFlight.size === 0) {
          nextSequence = remoteNext;
        } else {
          nextSequence = Math.max(nextSequence, remoteNext);
        }
      } else if (!mine && synchronizeSequence) {
        nextSequence = 1;
      }
      if (previouslyMine && !mine && inFlight.size > 0) {
        discardAmbiguousInput("Terminal control moved to another device while input was pending.");
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

    function discardAmbiguousInput(message = "The connection changed while input was in flight.") {
      if (inFlight.size === 0) {
        return;
      }
      let droppedBytes = 0;
      for (const frame of inFlight.values()) {
        droppedBytes += frame.bytes.length;
      }
      queuedBytes = Math.max(0, queuedBytes - droppedBytes);
      const droppedFrames = inFlight.size;
      inFlight.clear();
      setOverlay(`${message} ${droppedFrames} unacknowledged input frame${droppedFrames === 1 ? " was" : "s were"} not replayed automatically.`);
    }

    function sendAvailable() {
      if (!ready || !socketOpen()) {
        return;
      }
      if (socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
        scheduleSocketDrain();
        return;
      }
      while (queue.length > 0 && inFlight.size < MAX_IN_FLIGHT_FRAMES) {
        if (!Number.isSafeInteger(nextSequence) || nextSequence < 1) {
          throw new Error("Terminal input sequence is outside JavaScript's safe range.");
        }
        const frame = queue.shift();
        frame.sequence = nextSequence;
        nextSequence += 1;
        inFlight.set(frame.sequence, frame);
        socket.send(binaryFrame(frame.sequence, frame.bytes));
        if (socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
          scheduleSocketDrain();
          break;
        }
      }
    }

    function queueFrame(bytes) {
      if (!(bytes instanceof Uint8Array) || bytes.length === 0) {
        return;
      }
      queue.push({ bytes, sequence: null });
    }

    function flushInputBatch(dispatch = true) {
      window.clearTimeout(inputBatchTimer);
      inputBatchTimer = 0;
      if (batchBytes === 0) {
        if (dispatch) {
          sendAvailable();
        }
        return;
      }
      const combined = new Uint8Array(batchBytes);
      let cursor = 0;
      for (const chunk of batchChunks) {
        combined.set(chunk, cursor);
        cursor += chunk.length;
      }
      batchChunks = [];
      batchBytes = 0;
      for (let offset = 0; offset < combined.length; offset += MAX_FRAME_BYTES) {
        queueFrame(combined.slice(offset, Math.min(combined.length, offset + MAX_FRAME_BYTES)));
      }
      if (dispatch) {
        sendAvailable();
      }
    }

    function enqueue(bytes) {
      if (!(bytes instanceof Uint8Array) || bytes.length === 0 || disposed) {
        return;
      }
      if (queuedBytes + bytes.length > MAX_PENDING_BYTES) {
        setOverlay("Terminal input queue is full. Wait for the remote terminal to catch up.");
        return;
      }
      queuedBytes += bytes.length;
      for (let offset = 0; offset < bytes.length; offset += MAX_FRAME_BYTES) {
        const chunk = bytes.slice(offset, Math.min(bytes.length, offset + MAX_FRAME_BYTES));
        if (batchBytes > 0 && batchBytes + chunk.length > MAX_FRAME_BYTES) {
          flushInputBatch(false);
        }
        batchChunks.push(chunk);
        batchBytes += chunk.length;
        if (batchBytes >= MAX_FRAME_BYTES) {
          flushInputBatch(false);
        }
      }
      if (bytes.length >= 1024 || batchBytes >= 4096) {
        flushInputBatch(true);
      } else if (!inputBatchTimer) {
        inputBatchTimer = window.setTimeout(() => flushInputBatch(true), INPUT_BATCH_MS);
      }
    }

    function cancelOutputSchedule() {
      if (outputAnimationFrame) {
        window.cancelAnimationFrame(outputAnimationFrame);
        outputAnimationFrame = 0;
      }
      if (outputFlushTimer) {
        window.clearTimeout(outputFlushTimer);
        outputFlushTimer = 0;
      }
    }

    function flushTerminalOutput() {
      cancelOutputSchedule();
      if (outputBytes === 0) {
        return outputChain;
      }
      const chunks = outputChunks;
      const bytes = outputBytes;
      const generation = outputGeneration;
      outputChunks = [];
      outputBytes = 0;
      const combined = new Uint8Array(bytes);
      let cursor = 0;
      for (const chunk of chunks) {
        combined.set(chunk, cursor);
        cursor += chunk.length;
      }
      outputChain = outputChain.then(
        () => new Promise((resolve) => {
          if (disposed || generation !== outputGeneration) {
            resolve();
            return;
          }
          terminalApi.write(combined, resolve);
        }),
      );
      return outputChain;
    }

    function scheduleOutputFlush() {
      if (disposed || outputBytes === 0 || outputAnimationFrame || outputFlushTimer) {
        return;
      }
      if (outputBytes >= MAX_OUTPUT_BATCH_BYTES) {
        flushTerminalOutput();
        return;
      }
      if (document.visibilityState === "visible") {
        outputAnimationFrame = window.requestAnimationFrame(() => {
          outputAnimationFrame = 0;
          flushTerminalOutput();
        });
      } else {
        outputFlushTimer = window.setTimeout(() => {
          outputFlushTimer = 0;
          flushTerminalOutput();
        }, OUTPUT_HIDDEN_DELAY_MS);
      }
    }

    function queueTerminalReset(geometryValue) {
      const geometry = requiredGeometry(geometryValue);
      flushTerminalOutput();
      outputGeneration += 1;
      const generation = outputGeneration;
      outputChain = outputChain.then(() => {
        if (!disposed && generation === outputGeneration) {
          canonicalGeometry = geometry;
          terminalApi.fitGrid(geometry.cols, geometry.rows);
          terminalApi.reset();
        }
      });
      return generation;
    }

    function queueTerminalWrite(value) {
      const bytes = value instanceof Uint8Array ? value.slice() : new Uint8Array(value);
      if (bytes.length === 0 || disposed) {
        return outputChain;
      }
      if (outputBytes + bytes.length > MAX_OUTPUT_QUEUE_BYTES) {
        setOverlay("Terminal output exceeded the bounded browser queue. Reconnecting to resynchronize.");
        if (socketOpen()) {
          socket.close(4009, "terminal output backpressure");
        }
        return outputChain;
      }
      outputChunks.push(bytes);
      outputBytes += bytes.length;
      scheduleOutputFlush();
      return outputChain;
    }

    function afterTerminalOutput(generation, callback) {
      flushTerminalOutput();
      outputChain.then(() => {
        if (!disposed && generation === outputGeneration) {
          callback();
        }
      });
    }

    function finishReady(event) {
      const geometry = requiredGeometry(event.geometry);
      canonicalGeometry = geometry;
      terminalApi.fitGrid(geometry.cols, geometry.rows);
      applyControl(event.control, true);
      lastPongAt = Date.now();
      window.clearTimeout(stableTimer);
      stableTimer = window.setTimeout(() => {
        if (!disposed && ready && socketOpen()) {
          reconnectAttempt = 0;
        }
      }, STABLE_CONNECTION_MS);
      window.clearTimeout(helloTimer);
      setOverlay("");
      ready = true;
      window.requestAnimationFrame(() => {
        const focused = document.activeElement;
        if (!disposed && document.visibilityState === "visible" && document.hasFocus()
          && (!focused || focused === document.body || host.contains(focused))) {
          terminalApi.focus();
        }
      });
      flushInputBatch(false);
      sendAvailable();
      window.clearTimeout(webglTimer);
      webglTimer = window.setTimeout(() => terminalApi.activateWebgl(), 1200);
    }

    function applyServerEvent(event) {
      if (!isRecord(event) || typeof event.type !== "string") {
        throw new Error("Invalid terminal stream event.");
      }
      if (event.type === "reset") {
        queueTerminalReset(event.geometry);
        return;
      }
      if (event.type === "ready" || event.type === "resynced") {
        // The protocol is ready as soon as control/sequence state arrives. The
        // replay bytes are already ordered ahead of subsequent output by the
        // bounded output chain, so do not make typing wait for xterm to finish
        // painting a large history snapshot.
        finishReady(event);
        flushTerminalOutput();
        return;
      }
      if (event.type === "control") {
        applyControl(event.control);
        sendAvailable();
        return;
      }
      if (event.type === "ack") {
        const sequence = nonNegativeInteger(event.sequence, "terminal acknowledgement");
        const firstPending = inFlight.keys().next().value;
        if (firstPending !== sequence) {
          throw new Error("Terminal acknowledgement was out of order.");
        }
        const frame = inFlight.get(sequence);
        if (!frame) {
          throw new Error("Terminal acknowledgement did not match an in-flight frame.");
        }
        const remoteNext = remoteNextSequence(event.control);
        if (remoteNext !== null && remoteNext !== sequence + 1) {
          throw new Error("Terminal acknowledgement sequence did not advance exactly once.");
        }
        inFlight.delete(sequence);
        queuedBytes = Math.max(0, queuedBytes - frame.bytes.length);
        applyControl(event.control);
        sendAvailable();
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
      if (!networkOnline) {
        setStatus("Offline", "offline");
        return;
      }
      const ceiling = Math.min(MAX_RECONNECT_MS, 250 * 2 ** Math.min(reconnectAttempt, 5));
      const delay = Math.max(100, Math.floor(Math.random() * Math.max(101, ceiling)));
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
      const dimensions = boundedGeometry(terminalApi.fit());
      candidate.send(JSON.stringify({ type: "hello", cols: dimensions.cols, rows: dimensions.rows }));
      helloTimer = window.setTimeout(() => {
        if (candidate === socket && !ready) {
          candidate.close(4001, "terminal hello timeout");
        }
      }, 5000);
    }

    function resizeViewport() {
      if (disposed) {
        return;
      }
      fitViewport();
    }

    function connect() {
      if (disposed || !networkOnline) {
        if (!networkOnline) {
          setStatus("Offline", "offline");
        }
        return;
      }
      if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
        return;
      }
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(helloTimer);
      window.clearTimeout(stableTimer);
      ready = false;
      mine = false;
      setStatus("Connecting…", "connecting");
      const generation = socketGeneration + 1;
      socketGeneration = generation;
      const candidate = new WebSocket(streamUrl(), "termdeck.terminal.v1");
      candidate.binaryType = "arraybuffer";
      socket = candidate;
      const current = () => !disposed && generation === socketGeneration && candidate === socket;
      candidate.addEventListener("open", () => {
        if (!current()) {
          candidate.close(1000, "superseded terminal socket");
          return;
        }
        lastPongAt = Date.now();
        sendHelloWhenReady(candidate);
      });
      candidate.addEventListener("message", (message) => {
        if (!current()) {
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
      candidate.addEventListener("close", (event) => {
        if (!current()) {
          return;
        }
        flushInputBatch(false);
        flushTerminalOutput();
        socket = null;
        ready = false;
        mine = false;
        window.clearTimeout(helloTimer);
        window.clearTimeout(stableTimer);
        discardAmbiguousInput();
        const closeDetail = event.code === 1000
          ? "Terminal closed"
          : `Connection closed · ${event.code || "network"}`;
        setStatus(closeDetail, event.code === 1000 ? "connecting" : "reconnecting");
        scheduleReconnect();
      });
      candidate.addEventListener("error", () => {
        if (current()) {
          setStatus("Terminal connection failed", "error");
        }
      });
    }

    terminalApi.onData(enqueue);
    host.addEventListener("pointerdown", () => {
      terminalApi.focus();
      sendJson({ type: "focus" });
    });
    const observer = new ResizeObserver(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(resizeViewport, 80);
    });
    observer.observe(host);
    const reconnectNow = () => {
      networkOnline = true;
      reconnectAttempt = 0;
      window.clearTimeout(reconnectTimer);
      if (!socketOpen()) {
        connect();
      } else {
        lastPongAt = Date.now();
        sendJson({ type: "ping" });
      }
    };
    const pauseOffline = () => {
      networkOnline = false;
      window.clearTimeout(reconnectTimer);
      setStatus("Offline", "offline");
      if (socketOpen()) {
        socket.close(4005, "browser offline");
      }
    };
    const probeAfterResume = () => {
      if (document.visibilityState !== "visible" || !networkOnline) {
        return;
      }
      // Background tabs and suspended laptops may pause JavaScript timers for
      // minutes. Treat resume as a new liveness epoch rather than closing a
      // healthy server-side WebSocket because an old client timer fired late.
      lastPongAt = Date.now();
      reconnectAttempt = 0;
      window.clearTimeout(reconnectTimer);
      if (socketOpen()) {
        sendJson({ type: "ping" });
      } else {
        connect();
      }
    };
    window.addEventListener("online", reconnectNow);
    window.addEventListener("offline", pauseOffline);
    window.addEventListener("pageshow", probeAfterResume);
    window.addEventListener("focus", probeAfterResume);
    document.addEventListener("visibilitychange", probeAfterResume);
    heartbeatTimer = window.setInterval(() => {
      if (!ready || !socketOpen()) {
        return;
      }
      if (document.hidden) {
        // Browsers throttle or freeze hidden-page timers. Keep the application
        // socket open and let the proxy/server TCP keepalive own dead-peer
        // detection; probe immediately when the page becomes visible again.
        sendJson({ type: "ping" });
        return;
      }
      if (Date.now() - lastPongAt > HEARTBEAT_VISIBLE_TIMEOUT_MS) {
        socket.close(4002, "terminal heartbeat timeout");
        return;
      }
      sendJson({ type: "ping" });
    }, HEARTBEAT_INTERVAL_MS);
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
        window.clearTimeout(inputBatchTimer);
        window.clearTimeout(stableTimer);
        window.clearTimeout(socketDrainTimer);
        cancelOutputSchedule();
        window.clearInterval(heartbeatTimer);
        window.removeEventListener("online", reconnectNow);
        window.removeEventListener("offline", pauseOffline);
        window.removeEventListener("pageshow", probeAfterResume);
        window.removeEventListener("focus", probeAfterResume);
        document.removeEventListener("visibilitychange", probeAfterResume);
        queue = [];
        batchChunks = [];
        batchBytes = 0;
        queuedBytes = 0;
        inFlight.clear();
        outputChunks = [];
        outputBytes = 0;
        socketGeneration += 1;
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
    const homeLink = link("T1000", appPath("/"), "workspace-home");
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
    tabbar.append(node("span", "workspace-tabs-loading", "Syncing tabs…"));
    const shell = node("section", "workspace-shell");
    shell.dataset.state = "loading";
    const shellStatus = node("p", "workspace-state-message", "Syncing TermDeck…");
    shellStatus.setAttribute("role", "status");
    shellStatus.setAttribute("aria-live", "polite");
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
    shell.append(shellStatus, terminalIdentity, xtermShell);

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
    let refreshRequested = false;
    let disposed = false;
    let lastTabSignature = "";
    let lastActionSignature = "";

    function updateShellState(state, message) {
      shell.dataset.state = state;
      const messages = {
        loading: "Syncing TermDeck…",
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
      renderTabs(false);
      renderActions();
      if (samePane) {
        if (pane && envelope) {
          document.title = `${pane.title} · ${envelope.window.name} · TermDeck`;
        }
        terminalMount.focus();
        if (focusTab) {
          focusTabButton(paneId);
        }
        return;
      }
      if (terminalMount) {
        terminalMount.dispose();
        terminalMount = null;
      }
      terminalHost.replaceChildren();
      terminalMount = mountTerminal(routeSlug, paneId, terminalHost, terminalStatus, terminalOverlay);
      if (!focusTab) {
        terminalMount.focus();
      }
      if (pane && envelope) {
        document.title = `${pane.title} · ${envelope.window.name} · TermDeck`;
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

    function renderTabs(preserveFocus = true) {
      if (!envelope) {
        return;
      }
      windowTitle.textContent = envelope.window.name;
      workspaceStatus.textContent = `${envelope.window.panes.length} tabs`;
      const signature = JSON.stringify({
        activePaneId,
        windowName: envelope.window.name,
        panes: envelope.window.panes.map((pane) => ({
          id: pane.id,
          title: pane.title,
          cwd: pane.cwd,
          status: pane.status,
          sessionId: pane.sessionId,
          controllerActive: pane.controllerActive,
        })),
      });
      if (signature === lastTabSignature) {
        return;
      }
      const focusedPaneId = preserveFocus && document.activeElement?.getAttribute("role") === "tab"
        ? document.activeElement.dataset.paneId
        : "";
      lastTabSignature = signature;
      tabbar.replaceChildren(...envelope.window.panes.map(tabButton));
      if (focusedPaneId && envelope.window.panes.some((pane) => pane.id === focusedPaneId)) {
        focusTabButton(focusedPaneId);
      }
    }

    function actionButton(label, action, destructive) {
      const button = node("button", destructive ? "danger" : "", label);
      button.type = "button";
      button.addEventListener("click", () => runTabAction(action));
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
        sessionId: pane?.sessionId || null,
        status: pane?.status || "",
      });
      if (signature === lastActionSignature) {
        return;
      }
      lastActionSignature = signature;
      actionPanel.replaceChildren();
      actionPanel.append(actionLink("All windows", appPath("/")));
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
      const removedIndex = (action === "hide" || action === "kill") && envelope
        ? envelope.window.panes.findIndex((candidate) => candidate.id === pane.id)
        : -1;
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
          appPath(`/_termdeck/api/v1/windows/${encodeURIComponent(routeSlug)}/tabs`),
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
          updateShellState("empty");
        }
        workspaceStatus.textContent = response.headers.get("x-termdeck-stale") === "1"
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
