# TermDeck web client

This directory contains the Rust-first browser client for TermDeck/T1000. It exposes the same persistent tmux-backed terminals over an authenticated HTTPS origin while preserving the installed GTK application as a rollback-compatible peer until final cutover is independently approved.

## Browser experience

- `/` lists every remembered TermDeck window.
- `/<window-slug>` is the complete browser workspace for one TermDeck window: a live tab strip, one active terminal, a **New Tab** action, and tab lifecycle controls.
- `/<window-slug>/t/<pane-id>` is the stable deep link to one terminal. Selecting a tab updates this URL without losing the window shell.
- The terminal is rendered by a pinned xterm.js bundle. ANSI colors, cursor movement, alternate-screen applications, Unicode, IME input, mouse protocols, bracketed paste, selection, links, search, scrollback, and browser resizing are handled by a real terminal emulator rather than a text capture.
- Ordinary keyboard, paste, mouse, and resize events flow directly through an authenticated WebSocket. There is no separate send-text form and no synthetic Enter/Ctrl+C button panel.
- Controller leases are an internal safety mechanism. Focusing a vacant pane acquires it automatically; the first input on a remotely controlled pane explicitly takes it over. Many devices may remain viewers, but stale connections cannot write.
- New Tab creates a persisted T1000 session in the current window. The tab `×` behaves like native T1000 and hides the tab without stopping its process. Rename, pause, resume, pin, unpin, hide, and the separately confirmed **Kill process…** action use fixed `t1000` executable and argv contracts; no shell command interpolation is used.

## Transport and consistency model

The browser opens a same-origin WebSocket with subprotocol:

```text
termdeck.terminal.v1
```

The Rust backend attaches a dedicated tmux control-mode client to the selected session. Initial connection uses a pause/capture/continue transaction:

1. pause incremental pane output for that control client;
2. set the exact browser grid;
3. capture bounded scrollback, visible screen, terminal modes, alternate-screen state, and cursor position;
4. send one deterministic ANSI replay to xterm.js;
5. resume `%output`/`%extended-output` byte streaming.

Client input is binary and monotonically sequenced. Only the exact current device/pane/generation/lease/epoch authority may commit a sequence. An exact immediately preceding duplicate is acknowledged without re-execution when its digest-bound receipt exists. Gaps, changed duplicates, stale leases, and indeterminate outcomes fail closed. Ambiguous in-flight input is never replayed automatically after reconnect.

Viewer output, tmux command bodies, replay bytes, pending input, WebSocket sends, request concurrency, action concurrency, process output, and timeouts all have explicit bounds. A slow browser is disconnected rather than allowed to block tmux or grow host memory without limit.

## Authentication and LAN exposure

The current LAN deployment uses an HTTPS reverse proxy in front of the loopback Rust origin. Owner-password login creates opaque revocable device sessions with host-only `Secure`, `HttpOnly`, `SameSite=Strict` session cookies and a session-bound CSRF capability. Exact Host, Origin, Fetch Metadata, WebSocket subprotocol, and authenticated-session checks protect the browser surface.

The Rust origin itself remains loopback-only. Cloudflare Tunnel support is a later staging gate and must authenticate the origin rather than exposing the loopback service directly.

## Crates

- `termdeck-web-auth`: owner password, opaque device sessions, CSRF, revocation, audit chain, and bounded rate limiting in a separate owner-only database.
- `termdeck-web-core`: stable route allocation, immutable host/window/pane snapshots, and generation-safe one-controller-per-pane authority.
- `termdeck-web-state`: strict registry projection plus bounded read-only tmux inventory.
- `termdeck-web-server`: HTTP/JSON, authenticated WebSocket terminal streaming, xterm assets, fixed T1000 tab actions, cache and security middleware.

No crate uses unsafe Rust.

## Frontend build

The production runtime has no Node.js or Electron dependency. Node.js and esbuild are used only to create deterministic static assets embedded in the Rust binary.

```bash
cd web/frontend
npm ci --ignore-scripts
npm run build
```

Pinned package versions and integrity hashes live in `frontend/package-lock.json`. The generated `terminal-assets.json` binds the bundled JavaScript and CSS bytes. Third-party licenses are recorded in `THIRD_PARTY_NOTICES.md` and retained in the JavaScript bundle.

## Build and run

```bash
cd web
cargo build --release --locked -p termdeck-web-server
./target/release/termdeck-web-server \
  --db "$HOME/.local/share/termdeck/sessions.sqlite3" \
  --listen 127.0.0.1:7391 \
  --auth-dir "$HOME/.local/share/termdeck/web-auth" \
  --allow-insecure-loopback-auth \
  --tmux /usr/bin/tmux \
  --t1000 "$HOME/.local/bin/t1000"
```

The explicit insecure-loopback flag describes the internal HTTP hop only. Browser-facing LAN operation must terminate TLS before forwarding to that loopback origin.

## Verification

The authoritative source gate remains:

```bash
python scripts/check_web_readonly_smoke.py
```

Despite its historical filename, it now verifies the full authenticated client: Rust formatting/tests/Clippy, exact dependency boundaries, xterm bundle and license hashes, browser-only build output, authentication separation, WebSocket/control-mode contracts, input receipts, fixed T1000 argv, absence of obsolete manual-control UI, and absence of a production Node runtime.

Before promotion, the release must also pass:

- disposable real-tmux browser tests for ANSI/alternate-screen/Unicode, keyboard, paste, mouse, resize, reconnect, automatic control, and tab lifecycle operations;
- cross-host HTTPS browser validation;
- complete inherited T1000 production validation;
- byte and semantic invariance checks for the production registry, tmux panes, payload processes, authentication state, cwd, and recovery state;
- transactional binary/config installation and rollback evidence.

## Remaining cutover boundary

The web client may coexist with GTK while parity is being proven. Removing GTK/Python authority, enabling a public Cloudflare hostname, or changing the installed release remains prohibited until final staging demonstrates full action parity, performance budgets, secure origin handling, reboot persistence, rollback, and unchanged live-session semantics.

## Full browser terminal verification

The browser workspace mounts its initial server-rendered window snapshot immediately, then
reconciles in the background. Versioned terminal assets use a short private cache so opening
another tab does not download the xterm bundle again. Operator-facing controller controls are
intentionally absent: focusing and typing in a terminal performs generation-safe automatic
ownership transfer while every other authenticated browser remains a live viewer.

Run the complete disposable Chromium proof without addressing the production tmux server:

```bash
python3 scripts/check_web_terminal_e2e.py \
  --binary ./web/target/debug/termdeck-web-server \
  --evidence-dir ./.ai-bridge/web-terminal-e2e
```

It proves ANSI and alternate-screen replay, Unicode, keyboard controls, paste, SGR mouse,
resize, two-browser takeover and stale-frame rejection, reconnect/resynchronization, stable
window/tab routes, and the New Tab lifecycle.

For LAN deployment, keep the Rust server loopback-only and place a same-host HTTPS proxy in
front of it. `deploy/caddy/termdeck-web.Caddyfile.example` is the audited Caddy configuration;
Caddy forwards WebSocket upgrades automatically and the example fixes the upstream to HTTP/1.1.
The Host and Origin rewrite is deliberate: the Rust service accepts only loopback authority,
so the proxy must itself be local and the backend port must never be exposed to the LAN.
Validate the real TLS and `wss://` path on disposable ports with:

```bash
python3 scripts/check_web_https_proxy.py \
  --binary ./web/target/debug/termdeck-web-server \
  --evidence-dir ./.ai-bridge/web-https-proxy
```

The release load profile is fixed at 120 deterministic HTTP shards and 25 fresh authenticated
WebSocket process smokes. Every fixture uses a unique tmux socket, registry, authentication
store, port, and server process:

```bash
python3 scripts/check_web_production_profile.py \
  --binary ./web/target/release/termdeck-web-server \
  --evidence-dir ./.ai-bridge/web-production-profile
```

## HTTPS LAN reverse proxy

`deploy/caddy/termdeck-web.Caddyfile` is the portable Caddy TLS boundary. The current Max LAN deployment uses `deploy/nginx/termdeck-web.nginx.conf`: it binds `192.168.1.10:7443`, keeps the Rust service loopback-only, normalizes `Host` and `Origin`, and gives `/_termdeck/ws/` a dedicated HTTP/1.1 location with explicit `Upgrade`, `Connection`, and `Sec-WebSocket-Protocol` forwarding. Ordinary HTTP remains on a separate keep-alive path, while terminal streams disable buffering and receive one-hour read/send timeouts.

Validate before any reload:

```bash
caddy validate --adapter caddyfile --config deploy/caddy/termdeck-web.Caddyfile
nginx -t -p /warp/runtime/termdeck/termdeck-lan/nginx/ -c "$PWD/deploy/nginx/termdeck-web.nginx.conf"
```
