#!/usr/bin/env python3
"""Fail-closed static contract for the native TermDeck browser terminal UX."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "app": ROOT / "web/crates/termdeck-web-server/assets/app.js",
    "workspace_ui": ROOT / "web/crates/termdeck-web-server/assets/workspace-ui.js",
    "css": ROOT / "web/crates/termdeck-web-server/assets/app.css",
    "server": ROOT / "web/crates/termdeck-web-server/src/lib.rs",
    "stream": ROOT / "web/crates/termdeck-web-server/src/terminal_stream.rs",
    "terminal_http": ROOT / "web/crates/termdeck-web-server/src/terminal_http.rs",
    "terminal_source": ROOT / "web/frontend/src/terminal-entry.js",
    "palettes": ROOT / "web/frontend/src/terminal-palettes.json",
    "nginx": ROOT / "deploy/nginx/termdeck-web.nginx.conf",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(text: str, needle: str, surface: str) -> None:
    if needle not in text:
        raise SystemExit(f"missing {surface} contract: {needle}")


def reject(text: str, needle: str, surface: str) -> None:
    if needle in text:
        raise SystemExit(f"rejected {surface} contract remains: {needle}")


def main() -> None:
    text = {name: path.read_text(encoding="utf-8") for name, path in FILES.items()}
    server_production = text["server"].split("#[cfg(test)]\nmod tests {", 1)[0]
    stream_production = text["stream"].split("#[cfg(test)]", 1)[0]

    for needle in (
        "Take control",
        "Take over control",
        "Release control",
        "Send text",
        "Controller active",
        "typing switches control here",
    ):
        reject(text["app"], needle, "browser UI")
        reject(server_production, needle, "server-rendered UI")

    for needle in (
        "workspace-terminal-header",
        "Reconnecting in",
    ):
        reject(text["app"], needle, "native workspace UI")

    for needle in (
        'const routePane = root.getAttribute("data-termdeck-pane") || "";',
        "terminalMount = mountTerminal(routeSlug, paneId, terminalHost, terminalStatus, terminalOverlay);",
        "if (activePaneId) {",
        "switchPane(activePaneId, false);",
        "terminalApi.onData(enqueue)",
        "new ResizeObserver",
        "fitViewport()",
        'new WebSocket(streamUrl(), "termdeck.terminal.v1")',
        "binaryFrame(frame.sequence, frame.bytes)",
        'node("button", "workspace-new-tab", "+")',
        '["Rename tab", "rename"]',
        '["Hide tab", "hide"]',
        '["Kill process…", "kill"]',
        '["rename", "pause", "pin", "hide", "kill"].map(actionButton)',
        'actionConfirm.textContent = rename ? "Save name" : "Kill process"',
        'node("button", "workspace-tab-close", "×")',
        'sendJson({ type: "ping" })',
        'window.addEventListener("online", reconnectNow)',
        "let refreshRequested = false;",
        "queued ? 0 : document.hidden ? 30000 : 5000",
    ):
        require(text["app"], needle, "browser")

    for needle in (
        'data-termdeck-page=\\"window\\" data-termdeck-slug=\\"',
        'data-termdeck-pane=\\"',
        ".active_pane_id()",
        ".or_else(|| window.panes().first().map(PaneSnapshot::id))",
        "/_termdeck/assets/v8/app.css",
        "no-store, max-age=0",
    ):
        require(server_production, needle, "immediate pane bootstrap")

    for needle in (
        "location ^~ /_termdeck/ws/",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;",
        "proxy_read_timeout 86400s;",
        "proxy_request_buffering off;",
    ):
        require(text["nginx"], needle, "nginx WebSocket proxy")

    require(
        text["stream"],
        "capture-pane -epq -t {} -J -S -{} -E -1",
        "soft-wrap-preserving history replay",
    )
    require(stream_production, "MAX_INITIAL_REPLAY_HISTORY_LINES", "history depth bound")
    require(stream_production, "MAX_INITIAL_REPLAY_JOINED_CELLS", "joined-line geometry bound")
    require(stream_production, "initial_replay_history_lines", "geometry-aware replay selection")
    require(stream_production, "CONTROL_FLAGS_WRITER", "tmux writer mode")
    require(stream_production, 'CONTROL_FLAGS_WRITER: &str = "active-pane,ignore-size,no-detach-on-destroy"', "browser geometry isolation")
    reject(stream_production, "!ignore-size", "browser geometry authority")
    require(text["app"], "MAX_IN_FLIGHT_FRAMES = 64", "bounded pipelined input")
    require(text["app"], "INPUT_BATCH_MS = 3", "interactive input batching")
    require(text["app"], "inFlight = new Map()", "multi-frame acknowledgement tracking")
    require(text["terminal_source"], "fitGrid", "canonical-grid viewport fit")
    require(text["app"], "workspace-headerbar", "native compact workspace header")
    require(text["app"], "workspace-tab-close", "native tab close control")
    reject(text["app"], "workspace-topbar", "obsolete workspace header")
    reject(text["app"], "workspace-terminal-header", "obsolete terminal subheader")
    if text["css"].count("body.workspace-page {") != 1:
        raise SystemExit("workspace CSS must have exactly one authoritative body.workspace-page block")
    require(text["css"], ".workspace-headerbar {", "workspace header CSS")
    require(text["css"], "margin: 0;", "workspace header global-margin reset")
    require(text["css"], "@media (max-width: 460px)", "narrow viewport layout")
    require(
        text["stream"],
        "terminal.acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), true).await",
        "focus-driven generation-safe takeover",
    )
    require(stream_production, "send-keys -t {} -H", "exact-byte tmux input")
    require(text["terminal_http"], "MAX_CAPTURE_HISTORY_LINES", "bounded history")
    require(text["terminal_source"], "new Terminal", "xterm terminal")
    require(text["terminal_source"], "FitAddon", "terminal fit/resize")
    require(text["terminal_source"], "background: pack.background", "native terminal palette binding")
    require(text["terminal_source"], "cursor: pack.cursor", "native terminal cursor binding")
    signature_palette = json.loads(text["palettes"])["t1000"]
    if signature_palette["background"] != "#0a0d13" or signature_palette["cursor"] != "#82aaff":
        raise SystemExit("native signature terminal palette changed")

    payload = {
        "schema": "termdeck.web-interactive-contract.v2",
        "ok": True,
        "manual_controller_ui": False,
        "native_termdeck_chrome": True,
        "server_active_pane_bootstrap": True,
        "direct_xterm_input": True,
        "automatic_single_writer": True,
        "focus_takeover": True,
        "heartbeat_reconnect": True,
        "canonical_geometry_isolation": True,
        "bounded_pipelined_input": True,
        "nginx_websocket_upgrade": True,
        "tab_lifecycle": ["new", "rename", "pause", "resume", "pin", "unpin", "hide", "kill"],
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in FILES.values()},
    }
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
