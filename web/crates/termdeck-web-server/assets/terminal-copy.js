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
    const showCopyNotice = (message, retry) => {
      copyLabel.textContent = message;
      copyRetry.hidden = !retry;
      copyNotice.hidden = false;
      window.clearTimeout(copyNoticeTimer);
      if (!retry) copyNoticeTimer = window.setTimeout(() => { copyNotice.hidden = true; }, 1800);
    };
    const copySelection = () => {
      const selected = terminalApi.selection();
      if (!selected) return;
      if (!window.isSecureContext || !navigator.clipboard?.writeText) {
        showCopyNotice("Clipboard access unavailable. Use the browser Copy command.", false);
        return;
      }
      navigator.clipboard.writeText(selected).then(
        () => showCopyNotice("Selection copied", false),
        () => showCopyNotice("Clipboard permission denied. Use the browser Copy command.", true),
      );
    };
    copyRetry.addEventListener("click", copySelection);
    const selectionDown = event => {
      selectionStart = event.button === 0 ? {x: event.clientX, y: event.clientY} : null;
    };
    const selectionUp = event => {
      if (!selectionStart || event.button !== 0) return;
      const selectedByDrag = Math.abs(event.clientX - selectionStart.x) > 3
        || Math.abs(event.clientY - selectionStart.y) > 3;
      selectionStart = null;
      if (selectedByDrag || event.detail >= 2) copySelection();
    };
    host.addEventListener("pointerdown", selectionDown);
    host.addEventListener("pointerup", selectionUp);
    return () => {
      window.clearTimeout(copyNoticeTimer);
      host.removeEventListener("pointerdown", selectionDown);
      host.removeEventListener("pointerup", selectionUp);
      copyNotice.remove();
    };
  },
};
