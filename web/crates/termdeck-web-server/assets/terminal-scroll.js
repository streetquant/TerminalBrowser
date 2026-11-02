(() => {
  "use strict";
  const MAX_WHEEL_LINES = 120;
  const MAX_WHEEL_BATCH = 8;
  const WHEEL_MOUSE_MODES = new Set(["vt200", "drag", "any"]);

  function install(host, terminalApi, options = {}) {
    const terminal = terminalApi.terminal;
    if (!terminal || !host?.addEventListener) throw new TypeError("A terminal is required for scrolling.");
    const screen = terminal.element?.querySelector(".xterm-screen");
    const scrollbar = terminal.element?.querySelector(".xterm-scrollable-element > .scrollbar.vertical");
    // xterm owns the track, proportional thumb, pointer capture and viewport.
    // An application's internal document offset is not part of a terminal stream.
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
    const shell = host.parentElement;
    (shell?.classList.contains("xterm-shell") ? shell : host).append(live, status);
    let disposed = false, residual = 0, mouseResidual = 0, mouseTicks = 0;
    let noticeTimer = 0, updateFrame = 0, mouseFrame = 0;
    let wheelEvent = null, forwardedWheel = null, mouseGesture = null;
    let linePixels = 16, geometryObserver = null, renderedState = "";
    const subscriptions = [];
    const isController = () => options.isController?.() === true;
    const localBuffer = () => terminal.buffer.active.type === "normal";
    const maximum = () => localBuffer() ? Math.max(0, terminal.buffer.active.baseY) : 0;
    const hasMouseWheel = () => WHEEL_MOUSE_MODES.has(terminal.modes.mouseTrackingMode);
    const mouseMode = () => `${terminal.buffer.active.type}:${terminal.modes.mouseTrackingMode}`;
    const notice = (message) => {
      window.clearTimeout(noticeTimer);
      status.textContent = message;
      status.hidden = false;
      noticeTimer = window.setTimeout(() => { status.hidden = true; }, 2500);
    };
    const update = () => {
      if (disposed) return;
      const extent = maximum(), top = Math.min(extent, Math.max(0, terminal.buffer.active.viewportY));
      const owner = !localBuffer() || hasMouseWheel() ? "application" : "browser";
      const state = `${extent}:${top}:${owner}`;
      if (state === renderedState) return;
      renderedState = state;
      live.hidden = !localBuffer() || extent === top;
      host.dataset.termdeckScrollOwner = owner;
      host.dataset.termdeckHistoryLines = String(extent);
      if (scrollbar) {
        scrollbar.tabIndex = extent > 0 ? 0 : -1;
        scrollbar.setAttribute("aria-hidden", String(extent === 0));
        scrollbar.setAttribute("aria-valuemax", String(extent));
        scrollbar.setAttribute("aria-valuenow", String(top));
        scrollbar.setAttribute("aria-valuetext", extent === top ? "Live output" : `${extent - top} lines above live output`);
      }
    };
    const scheduleUpdate = () => {
      if (disposed || updateFrame) return;
      updateFrame = window.requestAnimationFrame(() => { updateFrame = 0; update(); });
    };
    const flushMouse = () => {
      mouseFrame = 0;
      if (disposed || !mouseGesture || !isController() || !hasMouseWheel() || mouseGesture.mode !== mouseMode()) {
        mouseTicks = 0; mouseGesture = null; return;
      }
      const count = Math.min(MAX_WHEEL_BATCH, Math.abs(mouseTicks)), direction = Math.sign(mouseTicks);
      mouseTicks -= direction * count;
      const dispatch = () => {
        for (let i = 0; i < count && isController() && mouseGesture?.mode === mouseMode(); i++) {
          forwardedWheel = new WheelEvent("wheel", {
            ...mouseGesture, bubbles: true, cancelable: true, deltaMode: 1, deltaY: direction,
          });
          try { screen.dispatchEvent(forwardedWheel); } finally { forwardedWheel = null; }
        }
      };
      if (options.onMouseWheelBatch) options.onMouseWheelBatch(dispatch);
      else dispatch();
      if (mouseTicks) mouseFrame = window.requestAnimationFrame(flushMouse);
      else mouseGesture = null;
    };
    const queueMouse = (ticks, event) => {
      if (!ticks || !screen || !isController()) return;
      if (mouseGesture && (mouseGesture.mode !== mouseMode() || Math.sign(mouseTicks) !== Math.sign(ticks))) mouseTicks = 0;
      mouseGesture = {mode: mouseMode(), clientX: event.clientX, clientY: event.clientY,
        ctrlKey: event.ctrlKey, altKey: event.altKey, shiftKey: event.shiftKey, metaKey: event.metaKey};
      mouseTicks = Math.max(-MAX_WHEEL_LINES, Math.min(MAX_WHEEL_LINES, mouseTicks + ticks));
      // Send the first response in this event turn. Only excess distance waits
      // for another frame; one wheel event must not add a frame of latency.
      if (!mouseFrame) flushMouse();
    };
    const consume = event => { event.preventDefault(); event.stopImmediatePropagation(); };
    const wheel = (event) => {
      if (disposed) return;
      wheelEvent = null;
      if (event === forwardedWheel) { wheelEvent = event; return; }
      if (event.ctrlKey || !Number.isFinite(event.deltaY) || event.deltaY === 0 || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.stopImmediatePropagation(); return;
      }
      const overflow = host.scrollHeight - host.clientHeight;
      if (host.dataset.termdeckOverflow === "true" && overflow > 1 &&
          (event.deltaY < 0 ? host.scrollTop > 0 : host.scrollTop < overflow - 1)) {
        consume(event);
        host.scrollTop += event.deltaMode === 1 ? event.deltaY * linePixels : event.deltaMode === 2 ? event.deltaY * host.clientHeight : event.deltaY;
        return;
      }
      if (hasMouseWheel() && !event.shiftKey) {
        consume(event); residual = 0;
        if (!isController()) { mouseResidual = 0; notice("Click the terminal to scroll this app."); return; }
        const amount = event.deltaMode === 1 ? event.deltaY : event.deltaMode === 2 ? event.deltaY * terminal.rows : event.deltaY / linePixels;
        if (Math.sign(mouseResidual) !== Math.sign(amount)) mouseResidual = 0;
        mouseResidual += Math.max(-MAX_WHEEL_LINES, Math.min(MAX_WHEEL_LINES, amount));
        const ticks = Math.trunc(mouseResidual); mouseResidual -= ticks;
        queueMouse(ticks, event); return;
      }
      if (!localBuffer()) {
        consume(event);
        if (event.shiftKey || typeof options.onApplicationScroll !== "function") { residual = 0; return; }
        if (!isController()) { residual = 0; notice("Click the terminal to scroll this app."); return; }
        const pixels = event.deltaMode === 1 ? event.deltaY * linePixels : event.deltaMode === 2 ? event.deltaY * 120 : event.deltaY;
        residual += Math.max(-360, Math.min(360, pixels));
        if (Math.abs(residual) >= 24) {
          options.onApplicationScroll(residual < 0 ? "up" : "down", Math.min(3, Math.max(1, Math.floor(Math.abs(residual) / 120))));
          residual = 0;
        }
        return;
      }
      // Ordinary history uses xterm's native wheel/trackpad path and scrollbar.
      // Shift bypasses an application's mouse protocol to inspect local history.
      if (hasMouseWheel()) {
        consume(event);
        const amount = event.deltaMode === 1 ? event.deltaY : event.deltaMode === 2 ? event.deltaY * terminal.rows : event.deltaY / linePixels;
        residual += Math.max(-MAX_WHEEL_LINES, Math.min(MAX_WHEEL_LINES, amount));
        const lines = Math.trunc(residual); residual -= lines;
        if (lines) terminal.scrollLines(lines);
      }
    };
    const guardViewerHover = (event) => {
      if (!isController() && event.buttons === 0 && terminal.modes.mouseTrackingMode === "any") event.stopImmediatePropagation();
    };
    const stopFocusIntent = event => event.stopPropagation();
    const scrollKey = event => {
      if (!localBuffer() || event.altKey || event.ctrlKey || event.metaKey) return;
      const lines = {ArrowUp: -1, ArrowDown: 1, PageUp: -terminal.rows, PageDown: terminal.rows}[event.key];
      if (event.key === "Home") terminal.scrollToTop();
      else if (event.key === "End") terminal.scrollToBottom();
      else if (lines) terminal.scrollLines(lines);
      else return;
      event.preventDefault(); event.stopPropagation();
    };
    const returnLive = () => terminal.scrollToBottom();
    if (scrollbar) {
      scrollbar.tabIndex = 0;
      scrollbar.setAttribute("role", "scrollbar");
      scrollbar.setAttribute("aria-label", "Terminal scroll position");
      scrollbar.setAttribute("aria-orientation", "vertical");
      scrollbar.setAttribute("aria-valuemin", "0");
      scrollbar.addEventListener("pointerdown", stopFocusIntent);
      scrollbar.addEventListener("keydown", scrollKey);
    }
    live.addEventListener("pointerdown", stopFocusIntent);
    live.addEventListener("click", returnLive);
    if (screen) {
      const remember = height => { if (Number.isFinite(height) && height > 0 && terminal.rows > 0) linePixels = Math.max(1, height / terminal.rows); };
      remember(screen.getBoundingClientRect().height);
      if (typeof window.ResizeObserver === "function") {
        geometryObserver = new window.ResizeObserver(entries => { for (const e of entries) if (e.target === screen) remember(e.contentRect?.height); });
        geometryObserver.observe(screen);
      }
    }
    host.addEventListener("wheel", wheel, {capture: true, passive: false});
    host.addEventListener("mousemove", guardViewerHover, true);
    subscriptions.push(terminal.onScroll(scheduleUpdate), terminal.onWriteParsed(scheduleUpdate));
    subscriptions.push(terminal.buffer.onBufferChange(() => {
      residual = mouseResidual = mouseTicks = 0; mouseGesture = null;
      window.cancelAnimationFrame(mouseFrame); mouseFrame = 0;
      renderedState = ""; update();
    }));
    update();
    return {
      isWheelInput() { return wheelEvent !== null && wheelEvent.eventPhase !== Event.NONE; },
      dispose() {
        if (disposed) return;
        disposed = true; wheelEvent = null;
        window.clearTimeout(noticeTimer); window.cancelAnimationFrame(updateFrame); window.cancelAnimationFrame(mouseFrame);
        geometryObserver?.disconnect();
        host.removeEventListener("wheel", wheel, true); host.removeEventListener("mousemove", guardViewerHover, true);
        scrollbar?.removeEventListener("pointerdown", stopFocusIntent); scrollbar?.removeEventListener("keydown", scrollKey);
        if (scrollbar) scrollbar.setAttribute("aria-hidden", "true");
        if (scrollbar) for (const name of ["tabindex", "role", "aria-label", "aria-orientation", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"]) scrollbar.removeAttribute(name);
        for (const subscription of subscriptions) subscription.dispose();
        live.remove(); status.remove();
        delete host.dataset.termdeckScrollOwner; delete host.dataset.termdeckHistoryLines;
      },
    };
  }
  window.T1000TerminalScroll = Object.freeze({install});
})();
