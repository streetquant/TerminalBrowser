import "@xterm/xterm/css/xterm.css";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";

const DEFAULT_FONT_SIZE = 13;
const MIN_FONT_SIZE = 7;

const DEFAULT_THEME = Object.freeze({
  background: "#0a0d13",
  foreground: "#dfe6f2",
  cursor: "#82aaff",
  cursorAccent: "#0a0d13",
  selectionBackground: "#82aaff55",
  selectionInactiveBackground: "#82aaff2f",
  black: "#0a0d13",
  red: "#f7768e",
  green: "#4fd6be",
  yellow: "#e0af68",
  blue: "#82aaff",
  magenta: "#c792ea",
  cyan: "#73daca",
  white: "#d8dee9",
  brightBlack: "#94a0b8",
  brightRed: "#ff8b96",
  brightGreen: "#9af0bd",
  brightYellow: "#ffe08a",
  brightBlue: "#9bbcff",
  brightMagenta: "#ddb0ff",
  brightCyan: "#9ce7dc",
  brightWhite: "#ffffff",
});

function byteStringToUint8Array(value) {
  const bytes = new Uint8Array(value.length);
  for (let index = 0; index < value.length; index += 1) {
    bytes[index] = value.charCodeAt(index) & 0xff;
  }
  return bytes;
}

export function createTerminal(element, options = {}) {
  if (!(element instanceof HTMLElement)) {
    throw new TypeError("A terminal host element is required.");
  }
  const terminal = new Terminal({
    allowProposedApi: false,
    allowTransparency: false,
    convertEol: false,
    cursorBlink: true,
    cursorStyle: "block",
    disableStdin: false,
    drawBoldTextInBrightColors: true,
    fastScrollModifier: "alt",
    fontFamily:
      options.fontFamily ||
      '"Cascadia Mono", "JetBrains Mono", "Iosevka Term", "SFMono-Regular", Consolas, "Liberation Mono", monospace',
    fontSize: Number.isFinite(options.fontSize) ? options.fontSize : DEFAULT_FONT_SIZE,
    fontWeight: "400",
    fontWeightBold: "700",
    letterSpacing: 0,
    lineHeight: 1.1,
    macOptionIsMeta: true,
    minimumContrastRatio: 1,
    overviewRulerWidth: 0,
    rightClickSelectsWord: false,
    scrollback: Number.isSafeInteger(options.scrollback) ? options.scrollback : 10000,
    smoothScrollDuration: 0,
    theme: { ...DEFAULT_THEME, ...(options.theme || {}) },
    windowsMode: false,
  });
  const fitAddon = new FitAddon();
  const searchAddon = new SearchAddon();
  const webLinksAddon = new WebLinksAddon(undefined, (_event, uri) => {
    const parsed = new URL(uri, window.location.href);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      window.open(parsed.href, "_blank", "noopener,noreferrer");
    }
  });
  terminal.loadAddon(fitAddon);
  terminal.loadAddon(searchAddon);
  terminal.loadAddon(webLinksAddon);
  terminal.open(element);

  let webglAddon = null;
  const activateWebgl = () => {
    if (options.webgl === false || webglAddon) {
      return;
    }
    try {
      webglAddon = new WebglAddon();
      webglAddon.onContextLoss(() => {
        try {
          webglAddon.dispose();
        } catch (_ignored) {
          // The canvas renderer remains authoritative after a context loss.
        }
        webglAddon = null;
      });
      terminal.loadAddon(webglAddon);
    } catch (_ignored) {
      webglAddon = null;
    }
  };
  const proposedDimensions = () => {
    const dimensions = fitAddon.proposeDimensions();
    if (
      !dimensions
      || !Number.isInteger(dimensions.cols)
      || !Number.isInteger(dimensions.rows)
      || dimensions.cols < 2
      || dimensions.rows < 2
    ) {
      return null;
    }
    return dimensions;
  };
  const fit = () => {
    const dimensions = proposedDimensions();
    if (dimensions) {
      terminal.resize(dimensions.cols, dimensions.rows);
    }
    return { cols: terminal.cols, rows: terminal.rows };
  };
  const fitGrid = (columns, rows) => {
    if (
      !Number.isInteger(columns)
      || !Number.isInteger(rows)
      || columns < 2
      || rows < 2
    ) {
      throw new TypeError("A canonical terminal grid is required.");
    }
    const maximum = Number.isFinite(options.fontSize)
      ? Math.max(MIN_FONT_SIZE, Math.floor(options.fontSize))
      : DEFAULT_FONT_SIZE;
    let chosen = MIN_FONT_SIZE;
    let fits = false;
    for (let fontSize = maximum; fontSize >= MIN_FONT_SIZE; fontSize -= 1) {
      terminal.options.fontSize = fontSize;
      const proposed = proposedDimensions();
      if (proposed && proposed.cols >= columns && proposed.rows >= rows) {
        chosen = fontSize;
        fits = true;
        break;
      }
    }
    terminal.options.fontSize = chosen;
    terminal.resize(columns, rows);
    element.dataset.termdeckColumns = String(columns);
    element.dataset.termdeckRows = String(rows);
    element.dataset.termdeckFontSize = String(chosen);
    element.dataset.termdeckOverflow = fits ? "false" : "true";
    return { cols: columns, rows, fontSize: chosen, fits };
  };
  const subscriptions = [];
  const api = {
    terminal,
    fit,
    fitGrid,
    dimensions() {
      return { cols: terminal.cols, rows: terminal.rows };
    },
    focus() {
      terminal.focus();
    },
    activateWebgl,
    write(data, callback) {
      terminal.write(data, callback);
    },
    reset() {
      terminal.reset();
    },
    clear() {
      terminal.clear();
    },
    paste(text) {
      terminal.paste(String(text));
    },
    selection() {
      return terminal.getSelection();
    },
    searchNext(query, searchOptions) {
      return searchAddon.findNext(String(query), searchOptions || {});
    },
    searchPrevious(query, searchOptions) {
      return searchAddon.findPrevious(String(query), searchOptions || {});
    },
    onData(callback) {
      const dataSubscription = terminal.onData((value) => callback(new TextEncoder().encode(value)));
      const binarySubscription = terminal.onBinary((value) => callback(byteStringToUint8Array(value)));
      subscriptions.push(dataSubscription, binarySubscription);
      return {
        dispose() {
          dataSubscription.dispose();
          binarySubscription.dispose();
        },
      };
    },
    onResize(callback) {
      const subscription = terminal.onResize(callback);
      subscriptions.push(subscription);
      return subscription;
    },
    readBuffer() {
      const active = terminal.buffer.active;
      const lines = [];
      for (let index = 0; index < active.length; index += 1) {
        lines.push(active.getLine(index)?.translateToString(true) || "");
      }
      return lines.join("\n");
    },
    dispose() {
      for (const subscription of subscriptions.splice(0)) {
        try {
          subscription.dispose();
        } catch (_ignored) {
          // Disposal is best effort during navigation.
        }
      }
      if (webglAddon) {
        try {
          webglAddon.dispose();
        } catch (_ignored) {
          // The terminal itself still owns final teardown.
        }
      }
      terminal.dispose();
    },
  };
  fit();
  return api;
}

export const terminalAssetVersion = "termdeck-xterm-v2";
