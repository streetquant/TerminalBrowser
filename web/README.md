# TermDeck web host

This directory contains the Rust-first replacement path for TermDeck's graphical frontend. The current implementation combines a strict read-only mirror of the existing TermDeck registry and tmux state with a bounded loopback HTTP server and a progressively enhanced browser frontend. Server-rendered HTML remains the no-JavaScript fallback; a small embedded vanilla-JavaScript client refreshes the same view from the versioned same-origin APIs without acquiring mutation authority.

It does **not** replace or control the installed GTK application yet. The existing T1000 0.2.19 runtime remains authoritative until later slices prove authenticated control, terminal-byte parity, action parity, installation, rollback, and live cutover.

## Current product contract

- One TermDeck host has one owner.
- Every device authenticated to that owner will be allowed to perform authorized actions in the later authenticated slice.
- A device is a **viewer for a pane** whenever it does not hold that pane's control lease. Viewer/controller is pane-local state, not a permanent device role.
- Any number of devices may view a pane.
- At most one device may control a pane at a time.
- Pane authority is bound to daemon generation, pane, device, lease identifier, monotonically increasing epoch, and expiry. Stale input, stale release, and pre-restart capabilities fail closed.
- `/` is the window homepage.
- Each mirrored window has a direct top-level route such as `/random`.
- All application-internal routes are reserved below `/_termdeck/`.

## Implemented boundary

The current server:

- opens `sessions.sqlite3` with SQLite `READ_ONLY` and `query_only` enforcement;
- rejects symlink and non-regular database paths;
- validates the existing rollback-compatible schema without creating or migrating anything;
- includes remembered empty windows and every visible, non-archived, non-stopped tab;
- overlays liveness and current working directory from one hard-coded read-only `tmux list-panes` command;
- preserves TermDeck's stable `session:window.pane` target rather than substituting tmux's ephemeral `%pane-id`;
- pre-renders immutable HTML and JSON buffers once per refresh;
- embeds one bounded CSS asset and one bounded vanilla-JavaScript asset in the Rust binary, with no Node.js production runtime;
- uses server-rendered HTML as the resilient fallback while the client validates and renders only the versioned same-origin API schemas;
- preserves the existing numeric API revision for compatibility and also publishes an exact decimal `revision_text`, avoiding JavaScript's integer precision limit for full `u64` revisions;
- writes dynamic browser text through DOM text nodes rather than HTML sinks and displays pane-local viewer mode without exposing credentials or controller capabilities;
- refreshes visible pages on a bounded interval, slows hidden-page polling, aborts timed-out requests, and keeps the last safe server-rendered state during client/network failure;
- uses a bounded request semaphore and bounded response, asset, tmux-output, database-row, timeout, and stale-state limits;
- refuses non-loopback binding until authenticated-device transport exists;
- accepts only `GET` and `HEAD` behavior;
- validates loopback `Host` and `Origin` authority;
- emits a restrictive content-security policy and no-store, anti-framing, MIME-sniffing, referrer, and permissions headers;
- serves a bounded stale snapshot only for the configured recovery interval, then returns `503`;
- logs only the first error in a consecutive refresh-failure episode.

The live Max verification used 9 windows and 149 visible panes, checked all 149 mirrored live pane identities against the installed release, served 64 concurrent viewers, consumed 6,648 KiB RSS with two threads, and left the SQLite bytes, complete tmux pane inventory, and installed semantic snapshot unchanged.

The enhanced frontend was additionally executed in real headless Chromium against the live homepage and a direct window URL. Both pages completed their API-driven refresh with zero browser stderr. A 128-request mixed homepage/asset/API load measured 8.088 ms median, 23.539 ms p95, and 37.475 ms maximum loopback latency; the host used 7,100 KiB RSS and two threads, with the same exact state-invariance result.

## Authentication foundation

`termdeck-web-auth` implements the security foundation for the next authenticated slice but is deliberately **not linked into the HTTP server yet**. Its boundary is:

- a separate schema-versioned SQLite database that can never use `sessions.sqlite3` or its sidecar names;
- absolute, traversal-free paths with non-symlink owner-controlled parents, exact `0700` directory and `0600` database permissions, hard-link rejection, and a 128 MiB file-plus-SQLite-page bound;
- strict table, column, index, trigger-body and foreign-key contracts, SQLite integrity checks, and exact host/owner identity binding;
- OS-CSPRNG one-time pairing invitations plus opaque device-session and CSRF capabilities;
- persistence of domain-separated SHA-256 digests and non-secret metadata only—raw capabilities are zeroized and never written to SQLite, audit rows, logs, or debug output;
- atomic invite consumption, session rotation, explicit logout, expiry, stale-row reclamation, device-wide revocation, and concurrent replay rejection;
- hard-capped hashed-subject rate limiting and a bounded, immutable, monotonic hash chain for tamper evidence; the chain is not claimed to be a keyed authenticity proof against a malicious same-user database writer;
- no HTTP, network, process, tmux, terminal, existing-registry, or controller-mutation authority.

The later HTTP-authentication slice must explicitly integrate this crate and pass its own cookie, pairing, Origin, Fetch Metadata, CSRF, browser, LAN, and revocation gates before mirrored host state is exposed beyond the existing loopback read-only mode.

## Routes

| Route | Purpose |
|---|---|
| `/` | Homepage linking every remembered window |
| `/<window-slug>` | Direct browser view for one window |
| `/_termdeck/api/v1/snapshot` | Versioned full host snapshot JSON |
| `/_termdeck/api/v1/windows/<window-slug>` | Versioned one-window snapshot JSON |
| `/_termdeck/assets/v1/app.css` | Embedded bounded browser stylesheet |
| `/_termdeck/assets/v1/app.js` | Embedded bounded API-driven browser renderer |
| `/_termdeck/healthz` | Process liveness |
| `/_termdeck/readyz` | Fresh-state readiness |

Unknown windows and internal routes return `404`. Mutation methods return `405`.

## Running the current slice

Build and run from this directory:

```bash
cargo build --release --locked
./target/release/termdeck-web-server \
  --db "$HOME/.local/share/termdeck/sessions.sqlite3" \
  --listen 127.0.0.1:7391
```

The production tmux server is owned by the user's systemd manager. A transient or installed user service is therefore the authoritative execution environment; a sandboxed shell may see a different default tmux socket.

The server intentionally rejects `0.0.0.0`, LAN addresses, and public addresses. LAN and Cloudflare Tunnel exposure remain blocked until owner-device authentication, secure cookies/WebAuthn, origin validation, session revocation, and the authenticated WebSocket protocol are implemented and reviewed.

## Crates

- `termdeck-web-auth`: isolated owner/device invitation, session, CSRF, revocation, hard-capped rate-limit, audit, and secure SQLite foundation; intentionally not consumed by the server yet.
- `termdeck-web-core`: dependency-free IDs, immutable snapshots, stable route allocation, and generation-safe pane leases.
- `termdeck-web-state`: strict read-only SQLite projection and optional read-only tmux inventory overlay.
- `termdeck-web-server`: current-thread Tokio/Axum HTTP server, immutable rendering cache, security middleware, server-rendered fallbacks, and embedded bounded browser assets.

No crate uses unsafe Rust. The production runtime has no Node.js, Electron, GTK, VTE, or Python server dependency. Node.js is used only as an optional validation-time JavaScript parser when it is already available; the Rust tests and static asset contract remain authoritative without it.

## Verification

The authoritative source gate is:

```bash
python scripts/check_web_readonly_smoke.py
```

It runs in bounded temporary Cargo and target directories and performs:

```bash
cargo fmt --all -- --check
RUSTFLAGS="-D warnings" cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- \
  -D warnings -D clippy::all -D clippy::unwrap_used -D clippy::expect_used
cargo tree --workspace --locked
cargo metadata --locked --no-deps --format-version 1
node --check crates/termdeck-web-server/assets/app.js  # when Node is available
```

The gate always enforces the asset byte budgets, UTF-8 encoding, same-origin API references, expected safe DOM operations, and absence of dangerous HTML/evaluation/storage sinks. It also pins the authentication crate's exact dependency set, library-only and unconsumed status, source-byte budget, CSPRNG/redaction/host-binding/strict-SQLite/resource-bound contracts, and absence of production HTTP, network, process, tmux, terminal-command, existing-registry, `SQLITE_OPEN_CREATE`, or premature server-consumer authority. The smoke is registered in `scripts/validation_manifest.json` for both CI and production profiles.

## Deliberately not implemented yet

The following remain behind later security and parity gates:

- persistent slug storage across daemon restarts and window renames;
- HTTP pairing, owner-device enrollment/revocation endpoints, and authenticated state-route protection;
- WebAuthn/passkeys, secure cookies, session introspection, and browser session management;
- LAN TLS and Cloudflare Access validation;
- terminal byte/WebSocket streaming;
- viewer-to-controller acquisition over HTTP/WebSocket;
- canonical terminal geometry and takeover replay;
- input, paste, mouse, clipboard, history, and scrollbar behavior;
- every management, process, account, workspace, diagnostic, release, and rollback action;
- GTK/Python runtime removal and production authority cutover.

No writable SQLite or tmux path may be introduced until those contracts have failing-first coverage, adversarial review, inherited production validation, and a rehearsed rollback boundary.
