#!/usr/bin/env python3
"""Check real rendered prompt contrast without connecting to a user's terminal."""

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import subprocess

from PIL import Image
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"
PALETTES = json.loads((ROOT / "web/frontend/src/terminal-palettes.json").read_text())


def luminance(rgb):
    channels = [v / 255 for v in rgb]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(first, second):
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


CASES = [
    ("prompt_rgb", "0;48;2;24;24;24;38;2;112;112;112", "Ask Codex to do anything HHHH", "#181818", 4.5),
    ("indexed_grey", "0;48;2;24;24;24;38;5;236", "HHHH Indexed prompt", "#181818", 4.5),
    ("ansi_black", "0;30", "HHHH ANSI prompt", None, 4.5),
    ("light_panel", "0;48;2;255;255;255;38;2;224;224;224", "HHHH Light panel", "#ffffff", 4.5),
    ("inverse", "0;48;2;32;32;32;38;2;24;24;24;7", "HHHH Inverse prompt", "#181818", 4.5),
    ("dim", "0;48;2;24;24;24;38;2;48;48;48;2", "HHHH Dim prompt", "#181818", 2.25),
    ("legible_unchanged", "0;48;2;24;24;24;38;2;230;230;230", "HHHH Existing readable text", "#181818", 4.5),
    ("graphic_unchanged", "0;48;2;24;24;24;38;2;48;48;48", "████████████", "#181818", None),
]
PAYLOAD = "\x1b[?25l" + "\r\n".join(
    f"\x1b[{sgr}m{text.ljust(72)}" for _, sgr, text, _, _ in CASES
) + "\x1b[0m"


def sample_rows(page):
    bounds = page.locator(".xterm-screen").bounding_box()
    image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    result = []
    for index, (name, _, _, background, minimum) in enumerate(CASES):
        top = round(bounds["y"] + index * bounds["height"] / 10)
        bottom = round(bounds["y"] + (index + 1) * bounds["height"] / 10)
        left = round(bounds["x"])
        # Written spaces retain reverse-video attributes; erase-to-EOL does not.
        actual_bg = image.getpixel((left + round(bounds["width"]) - 8, (top + bottom) // 2))
        pixels = Counter(image.crop((left, top + 2, left + 330, bottom - 2)).getdata())
        # Solid strokes of H and the other glyphs dominate non-background colors.
        foreground = max((color for color in pixels if max(abs(a - b) for a, b in zip(color, actual_bg)) > 2), key=lambda c: pixels[c])
        result.append({"case": name, "background": actual_bg, "foreground": foreground,
                       "ratio": contrast(foreground, actual_bg), "minimum": minimum})
    return result


def check(page, palette, expected_renderer, *, baseline=False):
    page.wait_for_function("kind => document.querySelector('#terminal').dataset.termdeckRenderer === kind",
                           arg=expected_renderer)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    rows = sample_rows(page)
    for row, case in zip(rows, CASES):
        expected_bg = rgb(case[3] or PALETTES[palette]["background"])
        # Software WebGL may dither an 8-bit channel by one step.
        assert max(abs(a - b) for a, b in zip(row["background"], expected_bg)) <= 1, (palette, expected_renderer, row)
        if not baseline and row["minimum"]:
            # Allow an 8-bit rasterization rounding step, not a lower policy floor.
            assert row["ratio"] >= row["minimum"] - 0.05, (palette, expected_renderer, row)
        if row["case"] == "legible_unchanged":
            assert max(abs(channel - 230) for channel in row["foreground"]) <= 1, row
        if row["case"] == "graphic_unchanged":
            assert max(abs(channel - 48) for channel in row["foreground"]) <= 1, row
    state = page.evaluate("""() => ({ratio: api.terminal.options.minimumContrastRatio,
        buffer: api.readBuffer(), cols: api.terminal.cols, rows: api.terminal.rows})""")
    assert "Ask Codex to do anything HHHH" in state["buffer"]
    assert (state["cols"], state["rows"]) == (72, 10)
    return {"palette": palette, "renderer": expected_renderer, "policy": state["ratio"], "rows": rows}


def check_cursor(page):
    for text in ("{}", '{"cursorBlink":true}', '{"cursorBlink":false}', "invalid JSON"):
        result = page.evaluate("""value => {
            localStorage.setItem('t1000.appearance', value);
            return T1000UI.preferences().cursorBlink;
        }""", text)
        assert result is False, text
    page.evaluate("""() => api.setAppearance({fontSize:20, cursorStyle:'bar', cursorBlink:true})""")
    assert page.evaluate("api.terminal.options.cursorBlink") is False
    for code, shape in ((1, "block"), (2, "block"), (3, "underline"), (4, "underline"), (5, "bar"), (6, "bar"), (0, "bar")):
        page.evaluate("text => new Promise(resolve => api.write(text, resolve))", f"\x1b[{code} q")
        state = page.evaluate("({blink:api.terminal.options.cursorBlink, shape:api.terminal.options.cursorStyle})")
        assert state == {"blink": False, "shape": shape}, (code, state)
    for sequence in ("\x1b[?12h", "\x1b[?1000;12;25h", "\x1b[?25;12h"):
        page.evaluate("text => new Promise(resolve => api.write(text, resolve))", sequence)
        assert page.evaluate("api.terminal.options.cursorBlink") is False, repr(sequence)
    # The combined mode guard must preserve mouse tracking, cursor visibility
    # and any actual output following the command in the same parser batch.
    assert page.evaluate("api.terminal.modes.mouseTrackingMode") != "none"
    page.evaluate("text => new Promise(resolve => api.write(text, resolve))", "\x1b[?1000l\x1b[?25h\x1b[9;1Hsteady HHHH")
    assert page.evaluate("api.terminal.modes.mouseTrackingMode") == "none"
    page.evaluate("api.focus(false)")
    page.wait_for_timeout(100)
    first = page.locator(".xterm-screen").screenshot()
    page.wait_for_timeout(750)
    assert page.locator(".xterm-screen").screenshot() == first, "cursor appearance changed while idle"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="2976f2e2351f61c7ccde2ef6989aeb1af0903a51")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline = subprocess.check_output(["git", "show", f"{args.baseline_ref}:web/crates/termdeck-web-server/assets/terminal.js"], cwd=ROOT)
    candidate = (ASSETS / "terminal.js").read_bytes()
    evidence = {"baseline_ref": args.baseline_ref, "candidate_sha256": hashlib.sha256(candidate).hexdigest(), "runs": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/brave", headless=True,
            args=["--disable-dev-shm-usage", "--enable-unsafe-swiftshader"])
        for renderer in ("dom", "webgl"):
            for phase, script in (("baseline", baseline), ("candidate", candidate)):
                page = browser.new_page(viewport={"width": 1000, "height": 400}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # Real software WebGL is functional coverage only. Production keeps
                # its hardware-only policy and adaptive renderer fallback intact.
                if renderer == "webgl":
                    page.add_init_script("""const original = WebGL2RenderingContext.prototype.getParameter;
                        WebGL2RenderingContext.prototype.getParameter = function(key) {
                            if (key === this.RENDERER || key === 37446) return 'Contrast fixture GPU';
                            return original.call(this, key);
                        };""")
                page.route("https://contrast.test/", lambda route: route.fulfill(content_type="text/html",
                    body="<html><body style='background:#000'><div id='terminal' style='width:980px;height:360px'></div></body></html>"))
                page.goto("https://contrast.test/")
                page.add_style_tag(path=str(ASSETS / "terminal.css"))
                page.add_script_tag(content=script.decode())
                page.add_script_tag(path=str(ASSETS / "workspace-ui.js"))
                page.evaluate("""({payload, webgl}) => {
                    window.api = TermDeckTerminal.createTerminal(document.querySelector('#terminal'),
                        {webgl, palette:'t1000', fontSize:20, cursorBlink:true});
                    api.terminal.resize(72, 10);
                    if (webgl) api.activateWebgl();
                    return new Promise(resolve => api.write(payload, resolve));
                }""", {"payload": PAYLOAD, "webgl": renderer == "webgl"})
                for palette in PALETTES:
                    # Read existing cells after a live palette change: no replay/reset.
                    page.evaluate("""palette => api.setAppearance({palette, fontSize:20,
                        cursorStyle:'block', cursorBlink:false})""", palette)
                    result = check(page, palette, renderer, baseline=phase == "baseline")
                    result["phase"] = phase
                    evidence["runs"].append(result)
                    if palette in ("t1000", "nord"):
                        page.screenshot(path=str(args.evidence_dir / f"{phase}-{renderer}-{palette}.png"))
                if phase == "candidate":
                    check_cursor(page)
                    evidence.setdefault("steady_cursor", []).append(renderer)
                assert not errors, errors
                api_disposed = page.evaluate("() => {api.dispose(); return document.querySelector('#terminal').children.length}")
                assert api_disposed == 0
                page.close()
        browser.close()
    baseline_prompt = next(r for r in evidence["runs"] if r["phase"] == "baseline")["rows"][0]
    assert baseline_prompt["ratio"] < 4.5, baseline_prompt
    evidence["ok"] = True
    (args.evidence_dir / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"ok": True, "configurations": len(evidence["runs"]),
        "candidate_prompt_ratio": next(r for r in evidence["runs"] if r["phase"] == "candidate")["rows"][0]["ratio"],
        "baseline_prompt_ratio": baseline_prompt["ratio"]}))


if __name__ == "__main__":
    main()
