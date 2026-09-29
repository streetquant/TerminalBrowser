(() => {
  "use strict";

  const MAX_WHEEL_LINES = 120;
  const WHEEL_MOUSE_MODES = new Set(["vt200", "drag", "any"]);

  function install(host, terminalApi, options = {}) {
    const terminal = terminalApi.terminal;
    if (!terminal || !host?.addEventListener) {
      throw new TypeError("A terminal is required for scrolling.");
    }
    const controls = document.createElement("div");
    controls.className = "terminal-scroll-controls";
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Terminal scrollback");
    const position = document.createElement("input");
    position.className = "terminal-scroll-position";
    position.type = "range";
    position.min = "0";
    position.max = "0";
    position.step = "1";
    position.value = "0";
    position.setAttribute("orient", "vertical");
    position.setAttribute("aria-label", "Terminal scroll position");
    const live = document.createElement("button");
    live.className = "terminal-scroll-live";
    live.type = "button";
    live.textContent = "↓";
    live.title = "Return to live output";
    live.setAttribute("aria-label", "Return to live output");
    const status = document.createElement("span");
    status.className = "terminal-scroll-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.hidden = true;
    const appUp = document.createElement("button");
    const appDown = document.createElement("button");
    for (const [button, label, text] of [[appUp, "Previous application page", "↑"], [appDown, "Next application page", "↓"]]) {
      button.type = "button";
      button.className = "terminal-scroll-page";
      button.setAttribute("aria-label", label);
      button.title = label;
      button.textContent = text;
      button.hidden = true;
    }
    controls.append(appUp, position, live, appDown);
    const shell = host.parentElement;
    (shell?.classList.contains("xterm-shell") ? shell : host).append(controls, status);

    let disposed = false;
    let residual = 0;
    let noticeTimer = 0;
    let updateFrame = 0;
    let renderedState = "";
    let wheelEvent = null;
    const subscriptions = [];
    const isController = () => options.isController?.() === true;
    const localBuffer = () => terminal.buffer.active.type === "normal";
    const maximum = () => localBuffer() ? Math.max(0, terminal.buffer.active.baseY) : 0;
    const hasMouseWheel = () => WHEEL_MOUSE_MODES.has(terminal.modes.mouseTrackingMode);
    const notice = (message) => {
      if (disposed) return;
      window.clearTimeout(noticeTimer);
      status.textContent = message;
      status.hidden = false;
      noticeTimer = window.setTimeout(() => { status.hidden = true; }, 3000);
    };
    const update = () => {
      if (disposed) return;
      const extent = maximum();
      const top = Math.min(extent, Math.max(0, terminal.buffer.active.viewportY));
      const owner = localBuffer() ? "browser" : "application";
      const pages = !localBuffer() && !hasMouseWheel() && typeof options.onApplicationScroll === "function";
      const snapshot = `${extent}:${top}:${owner}`;
      const state = `${snapshot}:${pages}`;
      if (state === renderedState) return;
      renderedState = state;
      controls.dataset.scrollPages = String(pages);
      position.hidden = pages;
      live.hidden = pages;
      appUp.hidden = appDown.hidden = !pages;
      position.max = String(extent);
      position.value = String(top);
      position.disabled = extent === 0;
      position.setAttribute("aria-valuetext", extent === top ? "Live output" : `${extent - top} lines above live output`);
      position.title = extent ? "Scroll terminal history" : localBuffer() ? "No scrollback available" : "This app owns its scrolling";
      live.disabled = extent === 0 || extent === top;
      host.dataset.termdeckScrollOwner = owner;
      host.dataset.termdeckHistoryLines = String(extent);
    };
    const applicationPage = (direction) => {
      if (localBuffer() || hasMouseWheel()) return;
      if (!isController()) {
        notice("Click the terminal to scroll this app.");
        return;
      }
      options.onApplicationScroll?.(direction);
    };
    const scheduleUpdate = () => {
      // xterm can emit onScroll for every parsed line in a large burst. Keep
      // DOM work bounded by painted frames, including repeated writes once
      // the retained history has reached its configured limit.
      if (disposed || updateFrame) return;
      updateFrame = window.requestAnimationFrame(() => { updateFrame = 0; update(); });
    };
    const consume = (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const wheel = (event) => {
      if (disposed) return;
      wheelEvent = null;
      // Browser zoom and horizontal overflow retain their native behavior,
      // while xterm must not turn either gesture into terminal input.
      if (event.ctrlKey || !Number.isFinite(event.deltaY) || event.deltaY === 0
          || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.stopImmediatePropagation();
        return;
      }
      const overflow = host.scrollHeight - host.clientHeight;
      if (host.dataset.termdeckOverflow === "true" && overflow > 1
          && (event.deltaY < 0 ? host.scrollTop > 0 : host.scrollTop < overflow - 1)) {
        consume(event);
        const pixels = event.deltaMode === 1 ? event.deltaY * 16
          : event.deltaMode === 2 ? event.deltaY * host.clientHeight : event.deltaY;
        host.scrollTop += pixels;
        return;
      }
      if (hasMouseWheel() && !event.shiftKey) {
        residual = 0;
        if (!isController()) {
          consume(event);
          notice("Click the terminal to scroll this app.");
          return;
        }
        // onData/onBinary fire synchronously during this wheel's
        // dispatch. The caller sends those reports in their own fenced frame,
        // without batching keyboard input or implicitly acquiring a lease.
        wheelEvent = event;
        return;
      }
      consume(event);
      if (!localBuffer()) {
        if (event.shiftKey || typeof options.onApplicationScroll !== "function") {
          residual = 0;
          return;
        }
        // Alternate-screen applications retain their own conversation. Page
        // navigation moves that surface; arrow keys would recall old prompts.
        const pixels = event.deltaMode === 1 ? event.deltaY * 16
          : event.deltaMode === 2 ? event.deltaY * 120 : event.deltaY;
        residual += Math.max(-360, Math.min(360, pixels));
        if (Math.abs(residual) >= 24) {
          const direction = residual < 0 ? "up" : "down";
          const pages = Math.min(3, Math.max(1, Math.floor(Math.abs(residual) / 120)));
          residual = 0;
          for (let i = 0; i < pages; i += 1) applicationPage(direction);
        }
        return;
      }
      const extent = maximum();
      if (!extent) {
        residual = 0;
        notice("No scrollback available yet.");
        return;
      }
      const screenHeight = terminal.element?.querySelector(".xterm-screen")?.getBoundingClientRect().height || 0;
      const lineHeight = screenHeight > 0 && terminal.rows > 0 ? screenHeight / terminal.rows : 16;
      const amount = event.deltaMode === 1 ? event.deltaY
        : event.deltaMode === 2 ? event.deltaY * terminal.rows : event.deltaY / lineHeight;
      residual += Math.max(-MAX_WHEEL_LINES, Math.min(MAX_WHEEL_LINES, amount));
      const lines = Math.trunc(residual);
      residual -= lines;
      if (lines) terminal.scrollLines(lines);
      update();
    };
    const changePosition = () => {
      const value = Number(position.value);
      if (!Number.isSafeInteger(value) || !localBuffer()) return;
      residual = 0;
      terminal.scrollToLine(Math.min(maximum(), Math.max(0, value)));
      update();
    };
    const returnLive = () => {
      residual = 0;
      terminal.scrollToBottom();
      update();
    };
    const guardViewerHover = (event) => {
      // Any-motion mouse mode reports hover before a wheel is dispatched.
      // Hover is passive; an actual click/drag retains the normal focus path.
      if (!isController() && event.buttons === 0 && terminal.modes.mouseTrackingMode === "any") {
        event.stopImmediatePropagation();
      }
    };
    // Scrolling controls are local: clicking or using their keyboard controls
    // must not bubble to the host's implicit terminal-focus/takeover handler.
    const stopFocusIntent = (event) => event.stopPropagation();
    controls.addEventListener("pointerdown", stopFocusIntent);
    controls.addEventListener("keydown", stopFocusIntent);
    controls.addEventListener("click", stopFocusIntent);
    position.addEventListener("input", changePosition);
    live.addEventListener("click", returnLive);
    appUp.addEventListener("click", () => applicationPage("up"));
    appDown.addEventListener("click", () => applicationPage("down"));
    host.addEventListener("wheel", wheel, { capture: true, passive: false });
    host.addEventListener("mousemove", guardViewerHover, true);
    subscriptions.push(terminal.onScroll(scheduleUpdate), terminal.onWriteParsed(scheduleUpdate));
    subscriptions.push(terminal.buffer.onBufferChange(() => {
      residual = 0;
      renderedState = "";
      update();
    }));
    update();

    return {
      isWheelInput() { return wheelEvent !== null && wheelEvent.eventPhase !== Event.NONE; },
      dispose() {
        if (disposed) return;
        disposed = true;
        wheelEvent = null;
        window.clearTimeout(noticeTimer);
        window.cancelAnimationFrame(updateFrame);
        host.removeEventListener("wheel", wheel, true);
        host.removeEventListener("mousemove", guardViewerHover, true);
        for (const subscription of subscriptions) subscription.dispose();
        controls.remove();
        status.remove();
        delete host.dataset.termdeckScrollOwner;
        delete host.dataset.termdeckHistoryLines;
      },
    };
  }

  window.T1000TerminalScroll = Object.freeze({ install });
})();
