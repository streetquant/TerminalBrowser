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
          this._render = new Set();
          this.parser = Object.fromEntries(['registerCsiHandler','registerEscHandler','registerDcsHandler']
            .map(name => [name, () => ({ dispose() {} })]));
          this.stats = { resizeDisposals: 0, dataDisposals: 0, binaryDisposals: 0, renderDisposals: 0 };
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
        onRender(callback) { return this._subscribe(this._render, callback, "renderDisposals"); }
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
          return { cols: globalThis.__termdeckWebglCols || 80, rows: 24 };
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
        activate(terminal) { terminal.webglAddon = this; globalThis.__termdeckWebglCols = 100; }
        onContextLoss(callback) { this.loss = callback; }
        dispose() { globalThis.__termdeckWebglCols = 80; }
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
  const overflowing = api.fitGrid(120, 40);
  assert.equal(api.terminal.options.fontSize, 16);
  assert.equal(overflowing.fits, false);
  assert.equal(host.dataset.termdeckOverflow, "true");
  assert.deepEqual(api.dimensions(), { cols: 120, rows: 40 });
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
  globalThis.document = { fonts: { status: "loaded" }, createElement: () => ({getContext: () => ({
    RENDERER: 1, getParameter: () => "NVIDIA GPU", getExtension: () => null,
  })}) };
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

function hardwareEnvironment() {
  const frames=new Map();let now=0,nextFrame=0,canceled=0;
  globalThis.window={devicePixelRatio:1,location:{href:"http://termdeck.test/"},
    performance:{now:()=>now},getComputedStyle:()=>({width:"800px",height:"600px"}),
    requestAnimationFrame(callback){frames.set(++nextFrame,callback);return nextFrame},
    cancelAnimationFrame(id){if(frames.delete(id))canceled++}};
  globalThis.document={hidden:false,fonts:{status:"loaded"},createElement:()=>({getContext:()=>({
    RENDERER:1,getParameter:()=>"Hardware GPU",getExtension:()=>null})})};
  globalThis.HTMLElement=class HTMLElement {};
  globalThis.__termdeckWebglCols=80;globalThis.__termdeckFitProposals=0;
  const host=new globalThis.HTMLElement();Object.assign(host,{clientWidth:800,clientHeight:600,dataset:{},style:{setProperty(){}}});
  return {host,frames,get canceled(){return canceled},advance(ms){now+=ms},
    tick(ms=16){now+=ms;const callbacks=[...frames.values()];frames.clear();for(const callback of callbacks)callback(now)}};
}

test("WebGL cell-width change refits the controller and context loss refits again", async () => {
  const env=hardwareEnvironment();
  const {createTerminal}=await loadTerminalEntry();let layoutChanges=0,api;
  api=createTerminal(env.host,{onLayoutChange(){layoutChanges++;api.fit()}});
  assert.deepEqual(api.dimensions(),{cols:80,rows:24});
  api.activateWebgl();env.tick();
  assert.deepEqual(api.dimensions(),{cols:100,rows:24});assert.equal(layoutChanges,1);
  const pending=env.frames.size;api.activateWebgl();assert.equal(env.frames.size,pending);
  api.terminal.webglAddon.loss();env.tick();
  assert.deepEqual(api.dimensions(),{cols:80,rows:24});assert.equal(layoutChanges,2);
  assert.equal(env.frames.size,0);assert.equal(env.canceled,1);api.dispose();
});

test("input encoding uses one shared encoder", async () => {
  const source = await readFile(sourcePath, "utf8");
  assert.equal((source.match(/new TextEncoder\(\)/g) || []).length, 1);
  assert.match(source, /TEXT_ENCODER\.encode\(value\)/);
});

for (const renderer of ["SwiftShader Device (Subzero)", "llvmpipe", "Microsoft Basic Render Driver", null]) {
  test(`software/unavailable WebGL keeps the responsive DOM renderer: ${renderer}`, async () => {
    globalThis.window = { devicePixelRatio: 1, location: { href: "http://termdeck.test/" },
      getComputedStyle: () => ({ width: "800px", height: "600px" }) };
    let probes=0, released=0;
    globalThis.document = { fonts: {status:"loaded"}, createElement: () => ({getContext: (_kind, opts) => {
      probes++; assert.equal(opts.failIfMajorPerformanceCaveat,true);
      return renderer===null ? null : {RENDERER:1, getParameter:()=>renderer,
        getExtension: name => name==='WEBGL_lose_context' ? {loseContext(){released++}} : null};
    }}) };
    globalThis.HTMLElement=class HTMLElement {};
    globalThis.__termdeckWebglCols=80;globalThis.__termdeckFitProposals=0;
    const {createTerminal}=await loadTerminalEntry();
    const host=new globalThis.HTMLElement();Object.assign(host,{clientWidth:800,clientHeight:600,dataset:{},style:{setProperty(){}}});
    const api=createTerminal(host);api.activateWebgl();api.activateWebgl();
    assert.equal(host.dataset.termdeckRenderer,'dom');assert.equal(api.terminal.webglAddon,undefined);
    assert.equal(probes,1);assert.equal(released,renderer===null?0:1);api.dispose();
  });
}


test("repeated slow render frames fall back once despite fast intervening frames; idle/hidden frames do not", async () => {
  const env=hardwareEnvironment();const {createTerminal}=await loadTerminalEntry();
  const api=createTerminal(env.host);api.activateWebgl();env.tick();
  const renderFrame=delay=>{env.advance(delay);for(const cb of api.terminal._render)cb();env.tick(0)};
  for(let i=0;i<5;i++)renderFrame(16);
  assert.equal(env.host.dataset.termdeckRenderer,"webgl","fast hardware remains enabled");
  for(let i=0;i<4;i++)env.tick(500);
  assert.equal(env.host.dataset.termdeckRenderer,"webgl","idle gaps are not rendering stalls");
  document.hidden=true;for(let i=0;i<4;i++)renderFrame(500);
  assert.equal(env.host.dataset.termdeckRenderer,"webgl","background throttling is ignored");
  document.hidden=false;renderFrame(16);
  const compositeStall=()=>{for(const cb of api.terminal._render)cb();env.tick(130)};
  compositeStall();renderFrame(16);compositeStall();renderFrame(16);
  assert.equal(env.host.dataset.termdeckRenderer,"webgl");compositeStall();
  assert.equal(env.host.dataset.termdeckRenderer,"dom");
  assert.equal(api.terminal._render.size,0);assert.equal(api.terminal.stats.renderDisposals,1);
  env.tick();api.activateWebgl();assert.equal(env.frames.size,0,"a rejected accelerator is not retried");
  api.dispose();assert.equal(api.terminal.stats.renderDisposals,1);
});

test("disposing an accelerated view cancels its renderer watchdog", async () => {
  const env=hardwareEnvironment();const {createTerminal}=await loadTerminalEntry();
  const api=createTerminal(env.host);api.activateWebgl();env.tick();assert.equal(env.frames.size,1);
  api.dispose();assert.equal(env.frames.size,0);assert.equal(api.terminal._render.size,0);assert.equal(env.canceled,1);
});

test("WebGL watchdog parks after idle and wakes on new output", async () => {
  const env = hardwareEnvironment();
  const { createTerminal } = await loadTerminalEntry();
  const api = createTerminal(env.host);
  api.activateWebgl();
  let callbacks = 0;
  for (let i = 0; i < 1000; i++) {
    callbacks += env.frames.size;
    env.tick(16);
  }
  assert.ok(callbacks <= 20, "idle watchdog callbacks must remain bounded");
  assert.equal(env.frames.size, 0, "idle terminal should not schedule another frame");
  for (const callback of api.terminal._render) callback();
  assert.equal(env.frames.size, 1, "render activity must rearm the watchdog");
  env.tick(16);
  assert.equal(env.frames.size, 1, "monitor must follow resumed output");
  api.dispose();
  assert.equal(env.frames.size, 0);
  assert.equal(api.terminal.stats.renderDisposals, 1);
});

test("WebGL capability probe is cached per document and invalidated by context loss", async () => {
  const env = hardwareEnvironment();
  let probes = 0, contextReleases = 0;
  document.createElement = () => ({ getContext: () => {
    probes++;
    return { RENDERER: 1, getParameter: () => "NVIDIA GPU", getExtension: name =>
      name === "WEBGL_lose_context" ? { loseContext() { contextReleases++; } } : null };
  } });
  const { createTerminal } = await loadTerminalEntry();
  const host = () => {
    const view = new HTMLElement();
    Object.assign(view, { clientWidth:800, clientHeight:600, dataset:{}, style:{setProperty(){}} });
    return view;
  };
  const first = createTerminal(host()), second = createTerminal(host());
  first.activateWebgl(); second.activateWebgl();
  assert.equal(probes, 1, "one shared hardware capability probe");
  assert.equal(contextReleases, 1, "temporary probe context is released");
  assert.ok(first.terminal.webglAddon && second.terminal.webglAddon);
  first.terminal.webglAddon.loss();
  env.tick();
  const third = createTerminal(host()); third.activateWebgl();
  assert.equal(probes, 2, "context loss invalidates the shared capability result");
  first.dispose(); second.dispose(); third.dispose();
  assert.equal(env.frames.size, 0, "all pending layout and monitor frames are cancelled");
});

test("software WebGL capability is shared without retaining a context", async () => {
  const env = hardwareEnvironment();
  let probes = 0;
  document.createElement = () => ({ getContext: () => {
    probes++;
    return { RENDERER: 1, getParameter: () => "llvmpipe", getExtension: () => null };
  } });
  const { createTerminal } = await loadTerminalEntry();
  const first = createTerminal(env.host);
  const second = createTerminal(env.host);
  first.activateWebgl(); second.activateWebgl();
  assert.equal(probes, 1);
  assert.equal(first.terminal.webglAddon, undefined);
  assert.equal(second.terminal.webglAddon, undefined);
  first.dispose(); second.dispose();
});

test("disposing a terminal cancels pending renderer layout callbacks", async () => {
  const env = hardwareEnvironment();
  const { createTerminal } = await loadTerminalEntry();
  let layoutCalls = 0;
  const api = createTerminal(env.host, { onLayoutChange() { layoutCalls++; } });
  api.activateWebgl();
  assert.ok(env.frames.size > 0);
  api.dispose();
  assert.equal(env.frames.size, 0);
  env.tick();
  assert.equal(layoutCalls, 0, "disposed terminals never receive stale layout notifications");
});
