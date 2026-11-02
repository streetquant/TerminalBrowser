"use strict";

// Each recent tab owns its renderer, bounded output queue and authenticated stream.
window.T1000TerminalSession = (() => {
  function mountTerminal(windowSlug, paneId, host, status, overlay, context) {
    const { appPath, isRecord, nonNegativeInteger, errorMessage } = context;
    const library = window.TermDeckTerminal;
    if (!host || !status || !overlay || !library || typeof library.createTerminal !== "function") {
      throw new Error("The interactive terminal assets did not load.");
    }

    const MAX_PENDING_BYTES = 1024 * 1024;
    const MAX_FRAME_BYTES = 32 * 1024;
    const MAX_IN_FLIGHT_FRAMES = 64;
    const INPUT_BATCH_MS = 3;
    const RESIZE_SETTLE_MS = 80;
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
    let active = true;
    let paneState = { status: "unknown", hasTerminal: false, terminalTarget: null };
    let recoveryInputNotice = "";
    let lastStatus = ["Connecting…", "connecting"];
    let appearanceKey = JSON.stringify(window.T1000UI.preferences());
    const terminalApi = library.createTerminal(host, {
      scrollback: 20000,
      ...window.T1000UI.preferences(),
      onLayoutChange: () => resizeViewport(),
      onFocusIntent: () => requestControl(),
    });
    Object.defineProperty(host, "termdeckTerminal", {configurable: true, value: terminalApi, writable: false});

    const disposeCopy = window.T1000TerminalCopy.install(host, terminalApi);

    let socket = null;
    let ready = false;
    let mine = false;
    let controlEpoch = null;
    let focusPending = false;
    let nextSequence = 1;
    let reconnectAttempt = 0;
    let reconnectTimer = 0;
    let resizeFrame = 0;
    let viewportResizeTimer = 0;
    let pendingResize = null;
    let resizeInFlight = false;
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
    let pendingOutputBytes = 0;
    let outputBackpressured = false;
    let outputWrites = 0;
    let canonicalGeometry = null;
    let lastViewportGeometry = null;
    let focusRequested = false;
    let wheelBatch = null;
    const scroll = window.T1000TerminalScroll.install(host, terminalApi,
      {isController: () => active && ready && mine && socketOpen(),
        onMouseWheelBatch: dispatchWheelBatch,
        onApplicationScroll: (direction, count = 1) => enqueueWheel(new TextEncoder().encode((direction === "up" ? "\x1b[5~" : "\x1b[6~").repeat(count)), 3)});

    function streamAllowed() {
      return paneState.hasTerminal && (paneState.status === "running" || paneState.status === "paused");
    }

    function recoveryMessage() {
      const messages = {
        "recovery-pending": ["Recovery pending", "This saved tab is waiting for recovery. Resolve its recovery blocker before relaunching."],
        missing: ["Recovery needed", "This saved tab has no live terminal. Its saved launch state must be recovered before connecting."],
        "start-error": ["Terminal could not start", "Check this tab’s saved launch settings before relaunching."],
        stopped: ["Terminal stopped", "This saved tab is stopped. It will connect after you explicitly relaunch it."],
        archived: ["Terminal archived", "Restore this saved tab before relaunching it."],
        dead: ["Terminal ended", "This terminal has ended. It will connect after it is relaunched."],
        new: ["Terminal starting", "Waiting for this tab’s live terminal to become available."],
        unknown: ["Checking terminal availability", "Waiting for a current terminal snapshot."],
      };
      return messages[paneState.status] || ["Waiting for recovery", "This saved tab has no live terminal yet. Waiting for recovery."];
    }

    function retireStream(reason) {
      const previous = socket;
      socketGeneration += 1;
      socket = null;
      clearViewportResize();
      ready = false;
      mine = false;
      controlEpoch = null;
      focusPending = false;
      focusRequested = false;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(helloTimer);
      window.clearTimeout(stableTimer);
      window.clearTimeout(socketDrainTimer);
      window.clearTimeout(inputBatchTimer);
      socketDrainTimer = 0;
      inputBatchTimer = 0;
      if (inFlight.size || queue.length || batchBytes) {
        recoveryInputNotice = " Pending input was discarded and will not be replayed automatically.";
      }
      discardAmbiguousInput();
      queue = [];
      batchChunks = [];
      batchBytes = 0;
      queuedBytes = 0;
      inFlight.clear();
      flushTerminalOutput();
      if (previous) previous.close(1000, reason);
    }

    function setPaneState(value) {
      if (disposed) return;
      const statuses = ["new", "running", "paused", "missing", "recovery-pending", "start-error", "dead", "stopped", "archived", "unknown"];
      if (!isRecord(value) || !statuses.includes(value.status) || typeof value.hasTerminal !== "boolean"
        || (value.terminalTarget != null && (typeof value.terminalTarget !== "string" || !value.terminalTarget.length))) {
        throw new Error("Invalid terminal availability state.");
      }
      const terminalTarget = value.terminalTarget ?? null;
      const wasAllowed = streamAllowed();
      const targetChanged = paneState.terminalTarget !== terminalTarget;
      const changed = paneState.status !== value.status || paneState.hasTerminal !== value.hasTerminal || targetChanged;
      paneState = { status: value.status, hasTerminal: value.hasTerminal, terminalTarget };
      if (streamAllowed()) {
        if (!wasAllowed || targetChanged) {
          if (wasAllowed && targetChanged) retireStream("saved terminal target changed");
          recoveryInputNotice = "";
          reconnectAttempt = 0;
          window.clearTimeout(reconnectTimer);
          setOverlay("");
          connect();
        }
        return;
      }
      if (changed || socket) retireStream("saved terminal unavailable");
      const [message, detail] = recoveryMessage();
      setStatus(message, "recovery");
      setOverlay(detail + recoveryInputNotice);
    }

    function requestControl() {
      if (!active || disposed || !streamAllowed()) return;
      if (ready && socketOpen()) {
        if (!focusPending) focusPending = sendJson({ type: "focus" });
      } else {
        focusRequested = true;
      }
    }

    function setStatus(message, kind) {
      lastStatus = [message, kind];
      if (!active) return;
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

    function scheduleViewportResize() {
      window.clearTimeout(viewportResizeTimer);
      viewportResizeTimer = 0;
      if (!pendingResize || resizeInFlight || !active || !ready || !mine || !socketOpen()) return;
      viewportResizeTimer = window.setTimeout(() => {
        viewportResizeTimer = 0;
        if (!pendingResize || resizeInFlight || !active || !ready || !mine || !socketOpen()) return;
        if (sendJson({ type: "resize", ...pendingResize })) {
          lastViewportGeometry = pendingResize;
          pendingResize = null;
          resizeInFlight = true;
        } else scheduleViewportResize();
      }, RESIZE_SETTLE_MS);
    }

    function clearViewportResize() {
      window.clearTimeout(viewportResizeTimer);
      viewportResizeTimer = 0;
      pendingResize = null;
      resizeInFlight = false;
    }

    function fitViewport() {
      if (!active) return canonicalGeometry || terminalApi.dimensions();
      const dimensions = boundedGeometry(terminalApi.fit());
      if (ready && mine && socketOpen()) {
        pendingResize = !lastViewportGeometry
          || lastViewportGeometry.cols !== dimensions.cols
          || lastViewportGeometry.rows !== dimensions.rows ? dimensions : null;
        // Dragging a sidebar used to send a full history replay per frame.
        // Fit locally immediately; publish the settled grid once and retain
        // only the newest geometry while the previous request is in flight.
        scheduleViewportResize();
      }
      if (canonicalGeometry && !mine) {
        return terminalApi.fitGrid(canonicalGeometry.cols, canonicalGeometry.rows);
      }
      return dimensions;
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
      if (!streamAllowed() || !socketOpen()) {
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
      const epoch = value.epoch === null || value.epoch === undefined
        ? null : nonNegativeInteger(value.epoch, "terminal control epoch");
      const changedLease = controlEpoch !== null && epoch !== controlEpoch;
      controlEpoch = epoch;
      mine = value.mine === true;
      if (!mine) clearViewportResize();
      else if (!previouslyMine && ready) resizeViewport();
      if (changedLease) discardAmbiguousInput("Terminal control generation changed while input was pending.");
      const remoteNext = remoteNextSequence(value);
      if (remoteNext !== null) {
        if (!inFlight.size && (changedLease || synchronizeSequence && !ready || !previouslyMine)) {
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

    function binaryFrame(sequence, bytes, type = 1) {
      const frame = new Uint8Array(9 + bytes.length);
      frame[0] = type;
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
      if (!streamAllowed() || !ready || !mine || focusPending || !socketOpen()) {
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
        socket.send(binaryFrame(frame.sequence, frame.bytes, frame.type));
        if (socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
          scheduleSocketDrain();
          break;
        }
      }
    }

    function queueFrame(bytes, type = 1) {
      if (!(bytes instanceof Uint8Array) || bytes.length === 0) {
        return;
      }
      queue.push({ bytes, sequence: null, type });
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
      let combined;
      if (batchChunks.length === 1) {
        [combined] = batchChunks;
      } else {
        combined = new Uint8Array(batchBytes);
        let cursor = 0;
        for (const chunk of batchChunks) {
          combined.set(chunk, cursor);
          cursor += chunk.length;
        }
      }
      batchChunks = [];
      batchBytes = 0;
      for (let offset = 0; offset < combined.length; offset += MAX_FRAME_BYTES) {
        queueFrame(combined.subarray(offset, Math.min(combined.length, offset + MAX_FRAME_BYTES)));
      }
      if (dispatch) {
        sendAvailable();
      }
    }

    function enqueue(bytes) {
      if (!streamAllowed()) return;
      if (!active || !(bytes instanceof Uint8Array) || bytes.length === 0 || disposed) {
        return;
      }
      if (queuedBytes + bytes.length > MAX_PENDING_BYTES) {
        setOverlay("Terminal input queue is full. Wait for the remote terminal to catch up.");
        return;
      }
      queuedBytes += bytes.length;
      for (let offset = 0; offset < bytes.length; offset += MAX_FRAME_BYTES) {
        const chunk = bytes.subarray(offset, Math.min(bytes.length, offset + MAX_FRAME_BYTES));
        if (batchBytes > 0 && batchBytes + chunk.length > MAX_FRAME_BYTES) {
          flushInputBatch(false);
        }
        batchChunks.push(chunk);
        batchBytes += chunk.length;
        if (batchBytes >= MAX_FRAME_BYTES) {
          flushInputBatch(false);
        }
      }
      if (bytes.length >= 1024 || batchBytes >= 4096 || (!inputBatchTimer && inFlight.size === 0)) {
        flushInputBatch(true);
      } else if (!inputBatchTimer) {
        inputBatchTimer = window.setTimeout(() => flushInputBatch(true), INPUT_BATCH_MS);
      }
    }

    function dispatchWheelBatch(dispatch) {
      const reports = [];
      wheelBatch = reports;
      try { dispatch(); } finally { wheelBatch = null; }
      // A homogeneous wheel burst remains passive, sequenced and mode-fenced.
      // One authorization/command/ack now carries up to eight native reports.
      for (let i = 0; i < reports.length;) {
        const first = reports[i];
        let count = 1;
        while (count < 8 && i + count < reports.length
            && reports[i + count].length === first.length
            && first.every((byte, offset) => reports[i + count][offset] === byte)) count++;
        const bytes = new Uint8Array(first.length * count);
        for (let n = 0; n < count; n++) bytes.set(first, n * first.length);
        enqueueWheel(bytes);
        i += count;
      }
    }

    function enqueueWheel(bytes, type = 2) {
      if (!streamAllowed()) return;
      if (!active || !ready || !mine || !socketOpen() || disposed || !(bytes instanceof Uint8Array)
          || !bytes.length || bytes.length > (type === 2 ? 512 : 32) || queuedBytes + bytes.length > MAX_PENDING_BYTES) return false;
      flushInputBatch(true);
      if (queue.length || inFlight.size >= MAX_IN_FLIGHT_FRAMES
          || socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) return false;
      queuedBytes += bytes.length;
      queueFrame(bytes, type);
      sendAvailable();
      return true;
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
      let combined;
      if (chunks.length === 1) {
        [combined] = chunks;
      } else {
        combined = new Uint8Array(bytes);
        let cursor = 0;
        for (const chunk of chunks) {
          combined.set(chunk, cursor);
          cursor += chunk.length;
        }
      }
      outputWrites += 1;
      outputChain = outputChain.then(
        () => new Promise((resolve) => {
          const done = () => {
            outputWrites -= 1;
            pendingOutputBytes = Math.max(0, pendingOutputBytes - bytes);
            resolve();
          };
          if (disposed || generation !== outputGeneration) {
            done();
            return;
          }
          terminalApi.write(combined, done);
        }),
      );
      return outputChain;
    }

    function scheduleOutputFlush() {
      if (disposed || outputBytes === 0 || outputAnimationFrame || outputFlushTimer) {
        return;
      }
      const visible = active && document.visibilityState === "visible";
      if (outputBytes >= MAX_OUTPUT_BATCH_BYTES || (visible && outputWrites === 0 && outputBytes <= 32768)) {
        flushTerminalOutput();
        return;
      }
      if (visible) {
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
      outputBackpressured = false;
      const generation = outputGeneration;
      outputChain = outputChain.then(() => {
        if (!disposed && generation === outputGeneration) {
          canonicalGeometry = geometry;
          fitCanonical(geometry);
          terminalApi.reset();
        }
      });
      return generation;
    }

    function queueTerminalWrite(value) {
      const bytes = value instanceof Uint8Array
        ? (value.byteOffset === 0 && value.byteLength === value.buffer.byteLength ? value : value.slice())
        : new Uint8Array(value);
      if (bytes.length === 0 || disposed || outputBackpressured) {
        return outputChain;
      }
      // Include writes waiting in outputChain and the currently parsing write;
      // counting only outputChunks lets a busy stream hide unlimited backlog.
      if (pendingOutputBytes + bytes.length > MAX_OUTPUT_QUEUE_BYTES) {
        outputBackpressured = true;
        setOverlay("Terminal output exceeded the bounded browser queue. Reconnecting to resynchronize.");
        if (socketOpen()) {
          socket.close(4009, "terminal output backpressure");
        }
        return outputChain;
      }
      outputChunks.push(bytes);
      outputBytes += bytes.length;
      pendingOutputBytes += bytes.length;
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

    function fitCanonical(geometry) {
      if (active) terminalApi.fitGrid(geometry.cols, geometry.rows);
      else terminalApi.terminal.resize(geometry.cols, geometry.rows);
    }

    function scheduleRenderer() {
      window.clearTimeout(webglTimer);
      webglTimer = window.setTimeout(() => {
        if (active && !disposed) terminalApi.activateWebgl();
      }, 1200);
    }

    function finishReady(event) {
      const geometry = requiredGeometry(event.geometry);
      canonicalGeometry = geometry;
      fitCanonical(geometry);
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
      scheduleViewportResize();
      if (active && focusRequested) {
        focusRequested = false;
        requestControl();
      }
      window.requestAnimationFrame(() => {
        const focused = document.activeElement;
        if (active && !disposed && document.visibilityState === "visible" && document.hasFocus()
          && (!focused || focused === document.body || host.contains(focused))) {
          terminalApi.focus(false);
        }
      });
      flushInputBatch(false);
      sendAvailable();
      scheduleRenderer();
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
        resizeInFlight = false;
        finishReady(event);
        flushTerminalOutput();
        return;
      }
      if (event.type === "control") {
        applyControl(event.control);
        if (event.focused === true) focusPending = false;
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
      if (disposed || !active || !streamAllowed()) {
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
      const dimensions = boundedGeometry(active ? terminalApi.fit() : lastViewportGeometry || terminalApi.dimensions());
      lastViewportGeometry = dimensions;
      candidate.send(JSON.stringify({ type: "hello", cols: dimensions.cols, rows: dimensions.rows }));
      helloTimer = window.setTimeout(() => {
        if (candidate === socket && !ready) {
          candidate.close(4001, "terminal hello timeout");
        }
      }, 5000);
    }

    function resizeViewport() {
      if (disposed || !active) {
        return;
      }
      fitViewport();
    }

    function connect() {
      if (!streamAllowed()) return;
      if (disposed || !active || !networkOnline) {
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
      clearViewportResize();
      ready = false;
      mine = false;
      controlEpoch = null;
      focusPending = false;
      setStatus("Connecting…", "connecting");
      lastViewportGeometry = boundedGeometry(terminalApi.fit());
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
        clearViewportResize();
        ready = false;
        mine = false;
        focusPending = false;
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

    terminalApi.onData(bytes => {
      if (scroll.isWheelInput()) {
        if (wheelBatch) wheelBatch.push(bytes);
        else enqueueWheel(bytes);
      } else enqueue(bytes);
    });
    host.addEventListener("pointerdown", () => {
      if (active && !disposed) terminalApi.focus();
    });
    const observer = new ResizeObserver(() => {
      if (resizeFrame) return;
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = 0;
        resizeViewport();
      });
    });
    observer.observe(host);
    const reconnectNow = () => {
      networkOnline = true;
      reconnectAttempt = 0;
      window.clearTimeout(reconnectTimer);
      if (!active) return;
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
      if (streamAllowed()) setStatus("Offline", "offline");
      if (socketOpen()) {
        socket.close(4005, "browser offline");
      }
    };
    const probeAfterResume = () => {
      if (!active || document.visibilityState !== "visible" || !networkOnline) {
        return;
      }
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
        sendJson({ type: "ping" });
        return;
      }
      if (Date.now() - lastPongAt > HEARTBEAT_VISIBLE_TIMEOUT_MS) {
        socket.close(4002, "terminal heartbeat timeout");
        return;
      }
      sendJson({ type: "ping" });
    }, HEARTBEAT_INTERVAL_MS);
    setPaneState(context.initialPaneState || { status: "running", hasTerminal: true });

    return {
      setPaneState,
      deactivate() {
        flushInputBatch(true);
        active = false;
        window.clearTimeout(viewportResizeTimer);
        viewportResizeTimer = 0;
        focusRequested = false;
        terminalApi.terminal.blur();
        window.cancelAnimationFrame(resizeFrame);
        resizeFrame = 0;
        window.clearTimeout(webglTimer);
        window.clearTimeout(reconnectTimer);
      },
      activate() {
        if (disposed) return;
        active = true;
        const preferences = window.T1000UI.preferences();
        const key = JSON.stringify(preferences);
        if (key !== appearanceKey) {
          terminalApi.setAppearance(preferences);
          appearanceKey = key;
        }
        setStatus(...lastStatus);
        resizeViewport();
        terminalApi.terminal.refresh(0, terminalApi.terminal.rows - 1);
        scheduleRenderer();
        if (!socketOpen()) connect();
      },
      dispose() {
        if (disposed) {
          return;
        }
        disposed = true;
        clearViewportResize();
        observer.disconnect();
        window.clearTimeout(reconnectTimer);
        window.cancelAnimationFrame(resizeFrame);
        resizeFrame = 0;
        window.clearTimeout(webglTimer);
        window.clearTimeout(helloTimer);
        window.clearTimeout(inputBatchTimer);
        window.clearTimeout(stableTimer);
        window.clearTimeout(socketDrainTimer);
        cancelOutputSchedule();
        window.clearInterval(heartbeatTimer);
        disposeCopy();
        scroll.dispose();
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
        }
        terminalApi.dispose();
      },
      focus(claimControl = true) {
        if (active && !disposed) terminalApi.focus(claimControl);
      },
    };
  }

  function createCache({ parent, before, initial, create, limit = 4 }) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 4) throw new Error("Invalid terminal cache bound");
    const entries = new Map();
    let current = null;
    let initialUsed = false;
    const count = () => { parent.dataset.termdeckCachedTerminals = String(entries.size); };
    const discard = (id, entry) => {
      entry.mount.dispose();
      entry.shell.remove();
      entries.delete(id);
      if (current === entry) current = null;
      count();
    };
    function view(paneId) {
      if (!initialUsed) {
        initialUsed = true;
        return { ...initial, paneId };
      }
      const shell = document.createElement("div"); shell.className = "xterm-shell"; shell.id = "terminal-shell";
      const host = document.createElement("div"); host.className = "xterm-host"; host.id = "terminal-xterm";
      host.dataset.termdeckTerminal = "1";
      const overlay = document.createElement("div"); overlay.className = "terminal-overlay"; overlay.id = "terminal-overlay"; overlay.hidden = true;
      shell.append(host, overlay);
      return { paneId, shell, host, overlay };
    }
    return {
      select(paneId) {
        if (current?.paneId === paneId) return current;
        if (current) { current.mount.deactivate(); current.shell.remove(); }
        let entry = entries.get(paneId);
        if (!entry) {
          if (entries.size >= limit) {
            const [id, oldest] = entries.entries().next().value;
            discard(id, oldest);
          }
          entry = view(paneId);
          parent.insertBefore(entry.shell, before);
          entry.mount = create(entry);
        } else {
          parent.insertBefore(entry.shell, before);
          entry.mount.activate();
        }
        entries.delete(paneId);
        entries.set(paneId, entry);
        current = entry;
        count();
        return entry;
      },
      updatePaneStates(panes) {
        for (const pane of panes) {
          entries.get(pane.id)?.mount.setPaneState?.(pane);
        }
      },
      prune(paneIds) {
        const visible = new Set(paneIds);
        for (const [id, entry] of entries) if (!visible.has(id)) discard(id, entry);
      },
      clear() {
        for (const [id, entry] of entries) discard(id, entry);
      },
    };
  }

  return { mount: mountTerminal, createCache };
})();
