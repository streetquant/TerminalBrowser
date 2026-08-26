# TermDeck web host

This directory contains the Rust-first replacement path for TermDeck's graphical frontend. The current implementation combines a strict read-only mirror of the existing TermDeck registry and tmux state with a bounded HTTP server, stable direct window routes, an embedded browser frontend, and optional owner-device authentication.

It does **not** replace or control the installed GTK application yet. The existing T1000 0.2.19 runtime remains authoritative until later slices prove controller, terminal-byte, action, installation, rollback, and live-cutover parity.

## Current product contract

- One TermDeck host has one owner.
- Every enrolled device belongs to that owner.
- A device is a viewer for a pane whenever it does not hold that pane's controller lease. Viewer/controller is pane-local state, not a permanent device role.
- Any number of devices may view a pane, while at most one may control it.
- Pane authority is generation-, pane-, device-, lease-, epoch-, and expiry-bound. Stale and pre-restart capabilities fail closed.
- `/` is the window homepage, and every mirrored window has a direct route such as `/random`.
- Internal routes are reserved below `/_termdeck/`.

## Implemented boundary

The current server:

- opens `sessions.sqlite3` with SQLite `READ_ONLY` and verified `query_only` enforcement;
- rejects symlink and non-regular database paths;
- validates the rollback-compatible registry schema without creating or migrating it;
- includes remembered empty windows and every visible, non-archived, non-stopped tab;
- overlays liveness and current working directory from one hard-coded read-only `tmux list-panes` command;
- preserves TermDeck's stable pane target instead of exposing tmux's ephemeral `%pane-id` as canonical identity;
- pre-renders bounded immutable HTML and JSON buffers once per refresh;
- embeds bounded CSS and vanilla JavaScript, with no Node.js production runtime;
- preserves exact decimal `u64` revisions through `revision_text`;
- writes dynamic browser content through safe DOM text operations rather than HTML sinks;
- uses bounded request, response, asset, tmux-output, database-row, timeout, and stale-state limits;
- serves only `GET` and `HEAD` in its unauthenticated read-only mode;
- optionally protects state with one-time pairing, opaque sessions, CSRF, logout, device inventory, and revocation;
- emits CSP, no-store, anti-framing, MIME-sniffing, referrer, and permissions headers;
- serves a stale snapshot only inside the configured bounded recovery interval, then returns `503`.

The Rust origin still refuses non-loopback binding. A TLS reverse proxy may expose it to the LAN while preserving the origin's loopback-only authority. Native LAN TLS remains a later product slice.

## Owner-device authentication

`termdeck-web-auth` is consumed only when the server is started with an explicit auth directory and `--allow-insecure-loopback-auth`. The default read-only mode does not open the auth store.

The authentication boundary includes:

- a separate schema-versioned SQLite database that cannot use the production registry path;
- owner-only `0700` directory and `0600` database permissions, symlink and hard-link rejection, and a 128 MiB database bound;
- exact host/owner binding, schema and trigger verification, foreign-key checks, integrity checks, row bounds, and audit-chain verification;
- OS-CSPRNG one-time invitations plus opaque session and CSRF capabilities;
- persistence of domain-separated digests only; raw capabilities are redacted and zeroized;
- one-time invite consumption, expiry, rotation, logout, device-wide revocation, replay rejection, and bounded rate limiting;
- host-only `SameSite=Strict` cookies, `HttpOnly` for the session cookie, and a browser-readable session-bound CSRF cookie;
- exact same-origin mutation metadata and CSRF cookie/header verification.

Authentication grants viewing only. It does not grant controller leases, terminal input, tmux mutation, process actions, or writes to `sessions.sqlite3`.

## Routes

| Route | Purpose |
|---|---|
| `/` | Homepage linking every remembered window |
| `/<window-slug>` | Direct browser view for one window |
| `/_termdeck/api/v1/snapshot` | Versioned full host snapshot JSON |
| `/_termdeck/api/v1/windows/<window-slug>` | Versioned one-window snapshot JSON |
| `/_termdeck/api/v1/session` | Current authenticated session metadata |
| `/_termdeck/api/v1/logout` | CSRF-protected current-session revocation |
| `/_termdeck/api/v1/devices` | Bounded owner-device inventory |
| `/_termdeck/api/v1/devices/<device-id>/revoke` | CSRF-protected device-wide revocation |
| `/_termdeck/pair` | One-time owner-device pairing form |
| `/_termdeck/assets/v1/app.css` | Embedded stylesheet |
| `/_termdeck/assets/v1/app.js` | Embedded API-driven browser renderer |
| `/_termdeck/healthz` | Process liveness |
| `/_termdeck/readyz` | Fresh-state readiness |

## Running locally

Build from this directory:

```bash
cargo build --release --locked -p termdeck-web-server
```

Create a one-time invitation:

```bash
AUTH_DIR="$HOME/.local/share/termdeck/web-auth"

./target/release/termdeck-web-server \
  --auth-dir "$AUTH_DIR" \
  --host-id termdeck-local-host \
  --owner-id termdeck-local-owner \
  --create-invitation "My browser"
```

Start the authenticated loopback origin:

```bash
./target/release/termdeck-web-server \
  --db "$HOME/.local/share/termdeck/sessions.sqlite3" \
  --listen 127.0.0.1:7391 \
  --auth-dir "$AUTH_DIR" \
  --host-id termdeck-local-host \
  --owner-id termdeck-local-owner \
  --allow-insecure-loopback-auth
```

The `insecure` name is intentional: a plain-HTTP loopback origin cannot honestly set `Secure` cookies. A LAN deployment must terminate TLS at a reviewed same-host reverse proxy, add `Secure` to both auth cookies, and keep the Rust origin inaccessible except through loopback. Public and Cloudflare exposure remain blocked until the native secure-origin and remote-perimeter gates are complete.

## Crates

- `termdeck-web-auth`: host/owner-bound invitations, sessions, CSRF, revocation, rate limiting, audit evidence, and secure SQLite state.
- `termdeck-web-core`: dependency-free IDs, immutable snapshots, stable route allocation, and generation-safe pane leases.
- `termdeck-web-state`: strict read-only registry projection and optional read-only tmux overlay.
- `termdeck-web-server`: current-thread Tokio/Axum HTTP server, immutable render cache, security middleware, auth integration, and embedded assets.

No crate uses unsafe Rust. The production runtime has no Node.js, Electron, GTK, VTE, or Python server dependency.

## Verification

The authoritative source gate is:

```bash
python scripts/check_web_readonly_smoke.py
```

It runs bounded temporary Cargo and target directories and performs formatting, warnings-denied workspace tests, strict Clippy, dependency-tree and metadata checks, optional JavaScript syntax validation, frontend sink checks, authentication dependency and authority checks, and an exact `termdeck-web-server` authentication-consumer boundary.

## Deliberately not implemented yet

The following remain behind later security and parity gates:

- native LAN TLS and automatic certificate lifecycle;
- WebAuthn/passkeys and richer browser device management;
- Cloudflare Access and origin JWT validation;
- authenticated per-pane controller HTTP/WebSocket transport;
- terminal byte streaming, replay, geometry, input, paste, mouse, clipboard, history, and scrollbar parity;
- management, process, account, workspace, diagnostic, release, and rollback actions;
- GTK/Python runtime removal and production authority cutover.

No writable production-registry or tmux path may be introduced without failing-first coverage, adversarial review, complete inherited production validation, and a rehearsed rollback boundary.
