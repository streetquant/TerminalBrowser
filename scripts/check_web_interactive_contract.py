#!/usr/bin/env python3
"""Fail-closed static contract for the native TermDeck browser terminal UX."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "app": ROOT / "web/crates/termdeck-web-server/assets/app.js",
    "server": ROOT / "web/crates/termdeck-web-server/src/lib.rs",
    "stream": ROOT / "web/crates/termdeck-web-server/src/terminal_stream.rs",
    "terminal_http": ROOT / "web/crates/termdeck-web-server/src/terminal_http.rs",
    "terminal_source": ROOT / "web/frontend/src/terminal-entry.js",
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
        "terminalApi.onResize",
        'new WebSocket(streamUrl(), "termdeck.terminal.v1")',
        "binaryFrame(inFlight.sequence, inFlight.bytes)",
        'node("button", "workspace-new-tab", "+")',
        'actionButton("Rename tab", "rename", false)',
        'actionButton("Hide tab", "hide", false)',
        'actionButton("Kill process…", "kill", true)',
        'node("button", "workspace-tab-close", "×")',
        'sendJson({ type: "ping" })',
        'window.addEventListener("online", reconnectNow)',
    ):
        require(text["app"], needle, "browser")

    for needle in (
        'data-termdeck-page=\\"window\\" data-termdeck-slug=\\"',
        'data-termdeck-pane=\\"',
        ".active_pane_id()",
        ".or_else(|| window.panes().first().map(PaneSnapshot::id))",
        "/_termdeck/assets/v2/app.css",
        "public, max-age=86400, immutable",
    ):
        require(server_production, needle, "immediate pane bootstrap")

    for needle in (
        "location ^~ /_termdeck/ws/",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;",
        "proxy_read_timeout 3600s;",
        "proxy_request_buffering off;",
    ):
        require(text["nginx"], needle, "nginx WebSocket proxy")

    require(
        text["stream"],
        "capture-pane -epq -t {} -J -S -{} -E -1",
        "soft-wrap-preserving history replay",
    )
    require(text["stream"], "MAX_INITIAL_REPLAY_HISTORY_LINES", "history depth bound")
    require(text["stream"], "MAX_INITIAL_REPLAY_JOINED_CELLS", "joined-line geometry bound")
    require(text["stream"], "initial_replay_history_lines", "geometry-aware replay selection")
    require(text["stream"], "CONTROL_FLAGS_WRITER", "tmux writer mode")
    require(
        text["stream"],
        "terminal.acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), true).await",
        "focus-driven generation-safe takeover",
    )
    require(text["stream"], "send-keys -t {} -H", "exact-byte tmux input")
    require(text["terminal_http"], "MAX_CAPTURE_HISTORY_LINES", "bounded history")
    require(text["terminal_source"], "new Terminal", "xterm terminal")
    require(text["terminal_source"], "FitAddon", "terminal fit/resize")
    require(text["terminal_source"], 'background: "#0a0d13"', "native terminal palette")
    require(text["terminal_source"], 'cursor: "#82aaff"', "native terminal cursor")

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
        "nginx_websocket_upgrade": True,
        "tab_lifecycle": ["new", "rename", "pause", "resume", "pin", "unpin", "hide", "kill"],
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in FILES.values()},
    }
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
