import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const sourcePath = new URL("../src/terminal-entry.js", import.meta.url);

function mockModulesPlugin() {
  const namespace = "termdeck-test";
  const modules = {
    xterm: `
      export class Terminal {
        constructor(options) {
          this.options = { ...options };
          this.cols = 80;
          this.rows = 24;
          this.buffer = { active: { length: 0, getLine: () => null } };
          this._data = new Set();
          this._binary = new Set();
          this._resize = new Set();
          this.stats = { resizeDisposals: 0, dataDisposals: 0, binaryDisposals: 0 };
        }
        loadAddon(addon) { addon.activate?.(this); }
        open(element) { this.element = { parentElement: element }; }
        resize(cols, rows) {
          this.cols = cols;
          this.rows = rows;
          this.stats.resizeCalls = (this.stats.resizeCalls || 0) + 1;
        }
        focus() {}
        write(_data, callback) { callback?.(); }
        reset() {}
        clear() {}
        paste(_text) {}
        getSelection() { return ""; }
        onData(callback) { return this._subscribe(this._data, callback, "dataDisposals"); }
        onBinary(callback) { return this._subscribe(this._binary, callback, "binaryDisposals"); }
        onResize(callback) { return this._subscribe(this._resize, callback, "resizeDisposals"); }
        _subscribe(set, callback, disposeStat) {
          set.add(callback);
          let active = true;
          return {
            dispose: () => {
              if (!active) return;
              active = false;
              set.delete(callback);
              this.stats[disposeStat] += 1;
            },
          };
        }
        dispose() { this.disposed = true; }
      }
    `,
    fit: `
      export class FitAddon {
        activate(terminal) { this.terminal = terminal; }
        proposeDimensions() {
          globalThis.__termdeckFitProposals += 1;
          return { cols: 80, rows: 24 };
        }
        dispose() {}
      }
    `,
    search: `
      export class SearchAddon {
        findNext() { return false; }
        findPrevious() { return false; }
        dispose() {}
      }
    `,
    links: `
      export class WebLinksAddon {
        dispose() {}
      }
    `,
    webgl: `
      export class WebglAddon {
        onContextLoss() {}
        dispose() {}
      }
    `,
    css: "",
  };
  return {
    name: "termdeck-terminal-entry-test-mocks",
    setup(pluginBuild) {
      pluginBuild.onResolve(
        { filter: /^@xterm\/xterm\/css\/xterm\.css$/ },
        () => ({ path: "css", namespace }),
      );
      pluginBuild.onResolve(
        { filter: /^@xterm\/xterm$/ },
        () => ({ path: "xterm", namespace }),
      );
      pluginBuild.onResolve(
        { filter: /^@xterm\/addon-fit$/ },
        () => ({ path: "fit", namespace }),
      );
      pluginBuild.onResolve(
        { filter: /^@xterm\/addon-search$/ },
        () => ({ path: "search", namespace }),
      );
      pluginBuild.onResolve(
        { filter: /^@xterm\/addon-web-links$/ },
        () => ({ path: "links", namespace }),
      );
      pluginBuild.onResolve(
        { filter: /^@xterm\/addon-webgl$/ },
        () => ({ path: "webgl", namespace }),
      );
      pluginBuild.onLoad(
        { filter: /.*/, namespace },
        (args) => ({ contents: modules[args.path], loader: "js" }),
      );
    },
  };
}

async function loadTerminalEntry() {
  const result = await build({
    absWorkingDir: new URL("..", import.meta.url).pathname,
    bundle: true,
    entryPoints: [sourcePath.pathname],
    format: "esm",
    plugins: [mockModulesPlugin()],
    platform: "browser",
    write: false,
  });
  const source = result.outputFiles[0].text;
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}

test("canonical fit suppresses stable remeasurement and invalidates font/layout changes", async (t) => {
  const styleValues = {
    width: "800px",
    height: "600px",
    paddingTop: "0px",
    paddingBottom: "0px",
    paddingLeft: "0px",
    paddingRight: "0px",
  };
  globalThis.window = {
    devicePixelRatio: 1,
    location: { href: "http://termdeck.test/" },
    getComputedStyle: () => ({ ...styleValues }),
  };
  const fontListeners = new Map();
  globalThis.document = {
    fonts: {
      status: "loaded",
      addEventListener(name, callback) { fontListeners.set(name, callback); },
      removeEventListener(name, callback) {
        if (fontListeners.get(name) === callback) fontListeners.delete(name);
      },
    },
  };
  globalThis.HTMLElement = class HTMLElement {};
  globalThis.__termdeckFitProposals = 0;
  const { createTerminal } = await loadTerminalEntry();
  class Host extends HTMLElement {
    constructor() {
      super();
      this.clientWidth = 800;
      this.clientHeight = 600;
      this.dataset = {};
      this.style = { setProperty(name, value) { this[name] = value; } };
    }
  }

  const host = new Host();
  const api = createTerminal(host, { webgl: false });
  assert.equal(host.style["--td-terminal-bg"], api.terminal.options.theme.background);
  const beforeFirstFit = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const firstFitProposals = globalThis.__termdeckFitProposals - beforeFirstFit;
  const beforeCachedFit = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const cachedFitProposals = globalThis.__termdeckFitProposals - beforeCachedFit;
  assert.ok(firstFitProposals > 0);
  assert.equal(cachedFitProposals, 0);

  const beforeFontEvent = globalThis.__termdeckFitProposals;
  fontListeners.get("loadingdone")();
  api.fitGrid(80, 24);
  assert.ok(globalThis.__termdeckFitProposals > beforeFontEvent);

  api.terminal.options.letterSpacing = 0.25;
  const beforeMetricChange = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const metricChangeProposals = globalThis.__termdeckFitProposals - beforeMetricChange;
  assert.ok(metricChangeProposals > 0);

  document.fonts.status = "loading";
  const beforeFontLoad = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const firstLoadingFit = globalThis.__termdeckFitProposals - beforeFontLoad;
  const beforeSecondFontLoad = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const secondLoadingFit = globalThis.__termdeckFitProposals - beforeSecondFontLoad;
  assert.ok(firstLoadingFit > 0);
  assert.ok(secondLoadingFit > 0);

  document.fonts.status = "loaded";
  styleValues.paddingLeft = "4px";
  const beforeLayoutChange = globalThis.__termdeckFitProposals;
  api.fitGrid(80, 24);
  const layoutChangeProposals = globalThis.__termdeckFitProposals - beforeLayoutChange;
  assert.ok(layoutChangeProposals > 0);
  const beforeAppearance = globalThis.__termdeckFitProposals;
  api.setAppearance({ fontSize: 16, cursorStyle: "bar", cursorBlink: false });
  assert.deepEqual(api.dimensions(), { cols: 80, rows: 24 });
  assert.equal(api.terminal.options.fontSize, 16);
  assert.equal(api.terminal.options.cursorStyle, "bar");
  assert.equal(api.terminal.options.cursorBlink, false);
  assert.ok(globalThis.__termdeckFitProposals > beforeAppearance);
  for (const fontSize of [-1, 0, 9, 73, Infinity, NaN, 12.5, "16"]) {
    assert.throws(() => api.setAppearance({ fontSize, cursorStyle: "bar", cursorBlink: true }), /Invalid terminal appearance/);
  }
  const originalPalette = { ...api.terminal.options.theme };
  const appearance = { fontSize: 16, cursorStyle: "bar", cursorBlink: false };
  api.setAppearance({ ...appearance, palette: "nord", font: "liberation" });
  assert.equal(host.style["--td-terminal-bg"], api.terminal.options.theme.background);
  assert.equal(api.terminal.options.theme.background, "#2e3440");
  assert.equal(api.terminal.options.theme.red, "#bf616a");
  assert.equal(api.terminal.options.fontFamily, '"Liberation Mono", monospace');
  assert.deepEqual([api.terminal.cols, api.terminal.rows], [80, 24]);
  api.setAppearance({ ...appearance, palette: "nord", fontFamily: "Test Mono", cursorColor: "#abcdef", scrollback: 30000 });
  assert.equal(api.terminal.options.fontFamily, "Test Mono, monospace");
  assert.equal(api.terminal.options.theme.cursor, "#abcdef");
  assert.equal(api.terminal.options.scrollback, 30000);
  assert.deepEqual([api.terminal.cols, api.terminal.rows], [80, 24]);
  for (const invalid of [{scrollback: -1}, {scrollback: true}, {scrollback: 500001}, {fontFamily: "x".repeat(129)}, {cursorColor: "red"}]) {
    assert.throws(() => api.setAppearance({...appearance, ...invalid}), /Invalid terminal appearance/);
  }
  for (const field of ["palette", "font"]) {
    for (const invalid of ["__proto__", "constructor", "missing", null]) {
      assert.throws(() => api.setAppearance({ ...appearance, [field]: invalid }), /Invalid terminal appearance/);
    }
  }
  api.setAppearance({ ...appearance, palette: "t1000", font: "auto" });
  assert.deepEqual(api.terminal.options.theme, originalPalette);
  api.fitGrid(120, 40);
  assert.equal(api.terminal.options.fontSize, 7);
  assert.deepEqual(api.fit(), { cols: 80, rows: 24 });
  assert.equal(api.terminal.options.fontSize, 16);
  assert.equal(host.dataset.termdeckColumns, "80");
  assert.equal(host.dataset.termdeckOverflow, "false");
  t.diagnostic(
    `fit proposals: first=${firstFitProposals}, stable=${cachedFitProposals}, `
      + `metric=${metricChangeProposals}, layout=${layoutChangeProposals}, `
      + `font-loading=${firstLoadingFit}/${secondLoadingFit}`,
  );
  api.dispose();
  assert.equal(fontListeners.size, 0);
});

test("terminal listeners are removed exactly once across individual and owner disposal", async () => {
  globalThis.window = {
    devicePixelRatio: 1,
    location: { href: "http://termdeck.test/" },
    getComputedStyle: () => ({
      width: "800px",
      height: "600px",
      paddingTop: "0px",
      paddingBottom: "0px",
      paddingLeft: "0px",
      paddingRight: "0px",
    }),
  };
  globalThis.document = { fonts: { status: "loaded" } };
  globalThis.HTMLElement = class HTMLElement {};
  globalThis.__termdeckFitProposals = 0;
  const { createTerminal } = await loadTerminalEntry();
  const host = new globalThis.HTMLElement();
  host.clientWidth = 800;
  host.clientHeight = 600;
  host.dataset = {};
  host.style = { setProperty(name, value) { this[name] = value; } };
  const api = createTerminal(host, { webgl: false });
  const resizeSubscription = api.onResize(() => {});
  const dataSubscription = api.onData(() => {});
  resizeSubscription.dispose();
  dataSubscription.dispose();
  api.dispose();
  assert.equal(api.terminal.stats.resizeDisposals, 1);
  assert.equal(api.terminal.stats.dataDisposals, 1);
  assert.equal(api.terminal.stats.binaryDisposals, 1);
});

test("input encoding uses one shared encoder", async () => {
  const source = await readFile(sourcePath, "utf8");
  assert.equal((source.match(/new TextEncoder\(\)/g) || []).length, 1);
  assert.match(source, /TEXT_ENCODER\.encode\(value\)/);
});
