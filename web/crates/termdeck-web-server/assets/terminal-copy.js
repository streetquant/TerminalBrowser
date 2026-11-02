"use strict";

window.T1000TerminalCopy = {
  install(host, terminalApi) {
    const element = (tag, className, content) => {
      const value = document.createElement(tag);
      value.className = className;
      if (content) value.textContent = content;
      return value;
    };
    const copyNotice = element("div", "terminal-copy-notice");
    const copyLabel = element("span", "", "");
    const copyRetry = element("button", "", "Copy selection");
    copyRetry.type = "button";
    copyNotice.hidden = true;
    copyRetry.hidden = true;
    copyNotice.append(copyLabel, copyRetry);
    host.append(copyNotice);
    let copyNoticeTimer = 0;
    let selectionStart = null;
    let selectionTimer = 0;
    let disposed = false;
    const showCopyNotice = (message, retry) => {
      if (disposed) return;
      copyLabel.textContent = message;
      copyRetry.hidden = !retry;
      copyNotice.hidden = false;
      window.clearTimeout(copyNoticeTimer);
      if (!retry) copyNoticeTimer = window.setTimeout(() => { copyNotice.hidden = true; }, 1800);
    };
    const copySelection = () => {
      if (disposed) return false;
      const selected = terminalApi.selection();
      if (!selected) return false;
      if (!window.isSecureContext || !navigator.clipboard?.writeText) {
        showCopyNotice("Clipboard access unavailable. Use the browser Copy command.", false);
        return true;
      }
      const denied = () => showCopyNotice("Clipboard permission denied. Use the browser Copy command.", true);
      try {
        Promise.resolve(navigator.clipboard.writeText(selected)).then(
          () => showCopyNotice("Selection copied", false), denied,
        );
      } catch {
        denied();
      }
      return true;
    };
    copyRetry.addEventListener("click", copySelection);
    const cancelSelection = () => {
      selectionStart = null;
      window.clearTimeout(selectionTimer);
    };
    const selectionDown = event => {
      cancelSelection();
      if (event.button === 0 && !event.target?.closest("button, input, select, a")) {
        selectionStart = {x: event.clientX, y: event.clientY};
      }
    };
    const finishSelection = event => {
      if (!selectionStart || event.button !== 0) return;
      const selectedByDrag = Math.abs(event.clientX - selectionStart.x) > 3
        || Math.abs(event.clientY - selectionStart.y) > 3;
      cancelSelection();
      if (selectedByDrag || event.detail >= 2) {
        // xterm finalizes mouse selection on document mouseup. At window
        // bubble time its selection is ready and the user gesture is active.
        if (!copySelection()) selectionTimer = window.setTimeout(copySelection, 0);
      }
    };
    const pointerUp = event => {
      // Pointerup precedes mouseup, and its detail does not count clicks.
      // Keep a fallback for pointer-only gestures; a real mouseup cancels it.
      if (selectionStart && event.button === 0) {
        selectionTimer = window.setTimeout(() => finishSelection(event), 0);
      }
    };
    host.addEventListener("pointerdown", selectionDown);
    host.addEventListener("mousedown", selectionDown);
    window.addEventListener("pointerup", pointerUp);
    window.addEventListener("mouseup", finishSelection);
    window.addEventListener("pointercancel", cancelSelection);
    window.addEventListener("blur", cancelSelection);
    return () => {
      disposed = true;
      cancelSelection();
      window.clearTimeout(copyNoticeTimer);
      host.removeEventListener("pointerdown", selectionDown);
      host.removeEventListener("mousedown", selectionDown);
      window.removeEventListener("pointerup", pointerUp);
      window.removeEventListener("mouseup", finishSelection);
      window.removeEventListener("pointercancel", cancelSelection);
      window.removeEventListener("blur", cancelSelection);
      copyRetry.removeEventListener("click", copySelection);
      copyNotice.remove();
    };
  },
};
