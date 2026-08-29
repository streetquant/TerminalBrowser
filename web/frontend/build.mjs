import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const outputDirectory = resolve(
  here,
  "../crates/termdeck-web-server/assets",
);
const javascriptOutput = resolve(outputDirectory, "terminal.js");
const cssOutput = resolve(outputDirectory, "terminal.css");
const metadataOutput = resolve(outputDirectory, "terminal-assets.json");

await mkdir(outputDirectory, { recursive: true });
const result = await build({
  absWorkingDir: here,
  bundle: true,
  entryPoints: ["src/terminal-entry.js"],
  format: "iife",
  globalName: "TermDeckTerminal",
  legalComments: "eof",
  logLevel: "warning",
  metafile: true,
  minify: true,
  outfile: javascriptOutput,
  platform: "browser",
  sourcemap: false,
  target: ["chrome120", "firefox121", "safari17"],
});

async function asset(path) {
  const bytes = await readFile(path);
  return {
    bytes: bytes.length,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

const manifest = {
  schema: "termdeck.frontend-assets.v1",
  builder: "esbuild@0.28.2",
  inputs: Object.keys(result.metafile.inputs).sort(),
  outputs: {
    "terminal.css": await asset(cssOutput),
    "terminal.js": await asset(javascriptOutput),
  },
  packages: {
    "@xterm/addon-fit": "0.11.0",
    "@xterm/addon-search": "0.16.0",
    "@xterm/addon-web-links": "0.12.0",
    "@xterm/addon-webgl": "0.19.0",
    "@xterm/xterm": "6.0.0",
  },
};
await writeFile(metadataOutput, `${JSON.stringify(manifest)}\n`, "utf8");
console.log(JSON.stringify(manifest));
