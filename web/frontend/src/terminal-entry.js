import "@xterm/xterm/css/xterm.css";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import palettePacks from "./terminal-palettes.json";

const DEFAULT_FONT_SIZE = 13;
const MIN_FONT_SIZE = 7;
const TEXT_ENCODER = new TextEncoder();

const FONTS = Object.freeze({
  auto: ['Automatic monospace', '"Cascadia Mono", "JetBrains Mono", "Iosevka Term", "SFMono-Regular", Consolas, "Liberation Mono", monospace'],
  system: ['System monospace', 'monospace'],
  jetbrains: ['JetBrains Mono', '"JetBrains Mono", monospace'],
  cascadia: ['Cascadia Mono', '"Cascadia Mono", monospace'],
  iosevka: ['Iosevka Term', '"Iosevka Term", monospace'],
  liberation: ['Liberation Mono', '"Liberation Mono", monospace'],
});
const ANSI_COLORS = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
  'brightBlack', 'brightRed', 'brightGreen', 'brightYellow', 'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite'];

function paletteTheme(name) {
  const pack = palettePacks[Object.hasOwn(palettePacks, name) ? name : 't1000'];
  return {
    background: pack.background, foreground: pack.foreground, cursor: pack.cursor,
    cursorAccent: pack.background, selectionBackground: `${pack.cursor}55`,
    selectionInactiveBackground: `${pack.cursor}2f`,
    ...Object.fromEntries(ANSI_COLORS.map((color, index) => [color, pack.palette[index]])),
  };
}

export function appearanceOptions() {
  return {
    palettes: Object.entries(palettePacks).map(([value, pack]) => ({ value, label: pack.label })),
    fonts: Object.entries(FONTS).map(([value, font]) => ({ value, label: font[0] })),
  };
}

function byteStringToUint8Array(value) {
  const bytes = new Uint8Array(value.length);
  for (let index = 0; index < value.length; index += 1) {
    bytes[index] = value.charCodeAt(index) & 0xff;
  }
  return bytes;
}

// A software WebGL context can be much slower than xterm's DOM renderer,
// especially for full-screen apps. A successful context alone is insufficient.
function acceleratedWebglAvailable() {
  let gl;
  try {
    gl = document.createElement("canvas").getContext("webgl2", {
      antialias: false, depth: false, failIfMajorPerformanceCaveat: true,
    });
    if (!gl) return false;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const renderer = String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
    return !/swiftshader|llvmpipe|softpipe|software|basic render/i.test(renderer);
  } catch (_ignored) {
    return false;
  } finally {
    // This probe owns its context; do not retain one per cached terminal.
    try { gl?.getExtension("WEBGL_lose_context")?.loseContext(); } catch (_ignored) { /* Best effort */ }
  }
}

export function createTerminal(element, options = {}) {
  if (!(element instanceof HTMLElement)) {
    throw new TypeError("A terminal host element is required.");
  }
  const terminal = new Terminal({
    allowProposedApi: false,
    allowTransparency: false,
    convertEol: false,
    cursorBlink: options.cursorBlink !== false,
    cursorStyle: ["block", "bar", "underline"].includes(options.cursorStyle) ? options.cursorStyle : "block",
    disableStdin: false,
    drawBoldTextInBrightColors: true,
    fastScrollModifier: "alt",
    fontFamily:
      options.fontFamily ||
      FONTS[Object.hasOwn(FONTS, options.font) ? options.font : 'auto'][1],
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
    theme: { ...paletteTheme(options.palette), ...(options.cursorColor ? {cursor: options.cursorColor} : {}), ...(options.theme || {}) },
    windowsMode: false,
  });
  const fitAddon = new FitAddon();
  const syncBackground = () => element.style.setProperty("--td-terminal-bg", terminal.options.theme.background);
  syncBackground();
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
  let webglBlocked = false;
  let renderMonitor = null;
  element.dataset.termdeckRenderer = "dom";
  const notifyRendererLayout = () => {
    // WebGL and canvas can measure a different cell width. Refit after the
    // renderer has had a frame to settle, so the controller advertises the
    // grid that actually fills its viewport to tmux.
    window.requestAnimationFrame(() => {
      invalidateFontMetrics();
      options.onLayoutChange?.();
    });
  };
  const useDomRenderer = () => {
    renderMonitor?.dispose();
    renderMonitor = null;
    try { webglAddon?.dispose(); } catch (_ignored) { /* DOM fallback */ }
    webglAddon = null;
    webglBlocked = true;
    element.dataset.termdeckRenderer = "dom";
    notifyRendererLayout();
  };
  const monitorRenderer = () => {
    let frame = 0;
    let previous = window.performance.now();
    let lastRender = -Infinity;
    let renders = 0;
    let previousRenders = 0;
    let wasHidden = document.hidden;
    let slowFrames = [];
    const rendered = terminal.onRender(() => { renders++; lastRender = window.performance.now(); });
    const sample = () => {
      frame = 0;
      if (!webglAddon) return;
      const now = window.performance.now();
      if (document.hidden || wasHidden) {
        slowFrames = [];
      } else if (renders !== previousRenders) {
        // Sampling only from onWriteParsed misses work performed before that
        // callback. Follow painted-frame gaps while this terminal is rendering
        // instead; the network is outside this local measurement.
        slowFrames.push(now - previous > 80);
        if (slowFrames.length > 6) slowFrames.shift();
        if (slowFrames.filter(Boolean).length >= 3) {
          useDomRenderer();
          return;
        }
      } else if (now - lastRender > 1000) {
        slowFrames = [];
      }
      wasHidden = document.hidden;
      previousRenders = renders;
      previous = now;
      frame = window.requestAnimationFrame(sample);
    };
    frame = window.requestAnimationFrame(sample);
    renderMonitor = { dispose() {
      rendered.dispose();
      if (frame) window.cancelAnimationFrame(frame);
      frame = 0;
    } };
  };
  const activateWebgl = () => {
    if (options.webgl === false || webglAddon || webglBlocked) {
      return;
    }
    if (!acceleratedWebglAvailable()) {
      webglBlocked = true;
      return;
    }
    try {
      webglAddon = new WebglAddon();
      webglAddon.onContextLoss(useDomRenderer);
      terminal.loadAddon(webglAddon);
      element.dataset.termdeckRenderer = "webgl";
      notifyRendererLayout();
      monitorRenderer();
    } catch (_ignored) {
      useDomRenderer();
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
    // A viewer may have reduced the font to show a larger canonical grid.
    // A browser writer measures its own viewport at the selected font size.
    const preferredFontSize = Number.isFinite(options.fontSize)
      ? options.fontSize : DEFAULT_FONT_SIZE;
    if (terminal.options.fontSize !== preferredFontSize) {
      terminal.options.fontSize = preferredFontSize;
    }
    const dimensions = proposedDimensions();
    if (
      dimensions
      && (terminal.cols !== dimensions.cols || terminal.rows !== dimensions.rows)
    ) {
      terminal.resize(dimensions.cols, dimensions.rows);
    }
    element.dataset.termdeckColumns = String(terminal.cols);
    element.dataset.termdeckRows = String(terminal.rows);
    element.dataset.termdeckFontSize = String(terminal.options.fontSize);
    element.dataset.termdeckOverflow = "false";
    return { cols: terminal.cols, rows: terminal.rows };
  };
  let lastGridFit = null;
  const invalidateFontMetrics = () => {
    lastGridFit = null;
  };
  const fontMetrics = () => ({
    family: terminal.options.fontFamily,
    size: terminal.options.fontSize,
    weight: terminal.options.fontWeight,
    boldWeight: terminal.options.fontWeightBold,
    letterSpacing: terminal.options.letterSpacing,
    lineHeight: terminal.options.lineHeight,
    fontsStatus: document.fonts?.status || "unavailable",
  });
  const fitGrid = (columns, rows) => {
    if (
      !Number.isInteger(columns)
      || !Number.isInteger(rows)
      || columns < 2
      || rows < 2
    ) {
      throw new TypeError("A canonical terminal grid is required.");
    }
    const getComputedStyle = typeof window.getComputedStyle === "function"
      ? window.getComputedStyle.bind(window)
      : null;
    const parentStyle = getComputedStyle ? getComputedStyle(element) : null;
    const terminalStyle = getComputedStyle && terminal.element
      ? getComputedStyle(terminal.element)
      : null;
    const layout = {
      width: element.clientWidth,
      height: element.clientHeight,
      devicePixelRatio: window.devicePixelRatio || 1,
      parentWidth: parentStyle?.width || "",
      parentHeight: parentStyle?.height || "",
      terminalPaddingTop: terminalStyle?.paddingTop || "",
      terminalPaddingBottom: terminalStyle?.paddingBottom || "",
      terminalPaddingLeft: terminalStyle?.paddingLeft || "",
      terminalPaddingRight: terminalStyle?.paddingRight || "",
    };
    const metrics = fontMetrics();
    if (
      lastGridFit
      && lastGridFit.columns === columns
      && lastGridFit.rows === rows
      && lastGridFit.layout.width === layout.width
      && lastGridFit.layout.height === layout.height
      && lastGridFit.layout.devicePixelRatio === layout.devicePixelRatio
      && lastGridFit.layout.parentWidth === layout.parentWidth
      && lastGridFit.layout.parentHeight === layout.parentHeight
      && lastGridFit.layout.terminalPaddingTop === layout.terminalPaddingTop
      && lastGridFit.layout.terminalPaddingBottom === layout.terminalPaddingBottom
      && lastGridFit.layout.terminalPaddingLeft === layout.terminalPaddingLeft
      && lastGridFit.layout.terminalPaddingRight === layout.terminalPaddingRight
      && lastGridFit.metrics.family === metrics.family
      && lastGridFit.metrics.size === metrics.size
      && lastGridFit.metrics.weight === metrics.weight
      && lastGridFit.metrics.boldWeight === metrics.boldWeight
      && lastGridFit.metrics.letterSpacing === metrics.letterSpacing
      && lastGridFit.metrics.lineHeight === metrics.lineHeight
      && lastGridFit.metrics.fontsStatus === metrics.fontsStatus
      && metrics.fontsStatus !== "loading"
      && terminal.cols === columns
      && terminal.rows === rows
      && terminal.options.fontSize === lastGridFit.fontSize
    ) {
      return { ...lastGridFit.result };
    }
    // A passive viewer keeps the writer's canonical grid. Honour this
    // browser's selected text size and expose overflow for scrolling instead
    // of silently shrinking the font until the setting appears ineffective.
    const chosen = Number.isFinite(options.fontSize)
      ? Math.max(MIN_FONT_SIZE, Math.floor(options.fontSize))
      : DEFAULT_FONT_SIZE;
    terminal.options.fontSize = chosen;
    const proposed = proposedDimensions();
    const fits = !!proposed && proposed.cols >= columns && proposed.rows >= rows;
    if (terminal.cols !== columns || terminal.rows !== rows) {
      terminal.resize(columns, rows);
    }
    element.dataset.termdeckColumns = String(columns);
    element.dataset.termdeckRows = String(rows);
    element.dataset.termdeckFontSize = String(chosen);
    element.dataset.termdeckOverflow = fits ? "false" : "true";
    const result = { cols: columns, rows, fontSize: chosen, fits };
    lastGridFit = {
      columns,
      rows,
      fontSize: chosen,
      layout,
      metrics: fontMetrics(),
      result,
    };
    return { ...result };
  };
  const subscriptions = new Set();
  const trackSubscription = (subscription) => {
    let active = true;
    const tracked = {
      dispose() {
        if (!active) {
          return;
        }
        active = false;
        subscriptions.delete(tracked);
        subscription.dispose();
      },
    };
    subscriptions.add(tracked);
    return tracked;
  };
  // This view renders a tmux pane's raw output, including queries tmux has
  // already answered. A second reply can arrive after an app exits and become
  // shell input. Consume query instructions in the output parser; never filter
  // input bytes, since a cursor reply can be identical to a real function key.
  for (const id of [
    { final: "c" }, { prefix: ">", final: "c" },
    { final: "n" }, { prefix: "?", final: "n" },
    { intermediates: "$", final: "p" },
    { prefix: "?", intermediates: "$", final: "p" },
  ]) {
    trackSubscription(terminal.parser.registerCsiHandler(id, () => true));
  }
  trackSubscription(terminal.parser.registerEscHandler({ final: "Z" }, () => true));
  trackSubscription(terminal.parser.registerDcsHandler({ intermediates: "$", final: "q" }, () => true));
  const fontSet = document.fonts;
  if (fontSet?.addEventListener) {
    const fontChanged = () => {
      invalidateFontMetrics();
      options.onLayoutChange?.();
    };
    fontSet.addEventListener("loadingdone", fontChanged);
    fontSet.addEventListener("loadingerror", fontChanged);
    trackSubscription({
      dispose() {
        fontSet.removeEventListener?.("loadingdone", fontChanged);
        fontSet.removeEventListener?.("loadingerror", fontChanged);
      },
    });
  }
  const api = {
    terminal,
    fit,
    fitGrid,
    dimensions() {
      return { cols: terminal.cols, rows: terminal.rows };
    },
    focus(claimControl = true) {
      terminal.focus();
      if (claimControl) options.onFocusIntent?.();
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
    clearSearch() {
      searchAddon.clearDecorations();
    },
    setAppearance(value) {
      if (!Number.isInteger(value.fontSize) || value.fontSize < 10 || value.fontSize > 72
          || !["block", "bar", "underline"].includes(value.cursorStyle)
          || typeof value.cursorBlink !== "boolean"
          || (value.palette !== undefined && !Object.hasOwn(palettePacks, value.palette))
          || (value.font !== undefined && !Object.hasOwn(FONTS, value.font))
          || (value.fontFamily !== undefined && (typeof value.fontFamily !== "string" || value.fontFamily.length > 128))
          || (value.cursorColor && !/^#[0-9a-f]{6}$/i.test(value.cursorColor))
          || (value.scrollback !== undefined && (!Number.isInteger(value.scrollback) || value.scrollback < 100 || value.scrollback > 500000))) {
        throw new TypeError("Invalid terminal appearance.");
      }
      const grid = lastGridFit && { columns: lastGridFit.columns, rows: lastGridFit.rows };
      options.fontSize = value.fontSize;
      terminal.options.cursorStyle = value.cursorStyle;
      terminal.options.cursorBlink = value.cursorBlink;
      if (value.palette !== undefined) terminal.options.theme = paletteTheme(value.palette);
      if (value.font !== undefined) terminal.options.fontFamily = FONTS[value.font][1];
      if (value.fontFamily) terminal.options.fontFamily = `${value.fontFamily}, monospace`;
      if (value.cursorColor) terminal.options.theme = {...terminal.options.theme, cursor: value.cursorColor};
      syncBackground();
      if (value.scrollback !== undefined) terminal.options.scrollback = value.scrollback;
      invalidateFontMetrics();
      if (grid) fitGrid(grid.columns, grid.rows);
      else terminal.options.fontSize = value.fontSize;
      options.onLayoutChange?.();
    },
    onData(callback) {
      const dataSubscription = terminal.onData((value) => callback(TEXT_ENCODER.encode(value)));
      const binarySubscription = terminal.onBinary((value) => callback(byteStringToUint8Array(value)));
      return trackSubscription({
        dispose() {
          try {
            dataSubscription.dispose();
          } finally {
            binarySubscription.dispose();
          }
        },
      });
    },
    onResize(callback) {
      return trackSubscription(terminal.onResize(callback));
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
      renderMonitor?.dispose();
      renderMonitor = null;
      for (const subscription of subscriptions) {
        try {
          subscription.dispose();
        } catch (_ignored) {
          // Disposal is best effort during navigation.
        }
      }
      subscriptions.clear();
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
