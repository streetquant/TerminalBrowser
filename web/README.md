# TermDeck web host

This directory contains the Rust-first replacement path for TermDeck's graphical frontend. The current implementation is the first regression-safe vertical slice: a strict read-only mirror of the existing TermDeck registry and tmux state, exposed through a bounded loopback HTTP server and a minimal no-JavaScript browser view.

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
- uses a bounded request semaphore and bounded response, tmux-output, database-row, timeout, and stale-state limits;
- refuses non-loopback binding until authenticated-device transport exists;
- accepts only `GET` and `HEAD` behavior;
- validates loopback `Host` and `Origin` authority;
- emits a restrictive content-security policy and no-store, anti-framing, MIME-sniffing, referrer, and permissions headers;
- serves a bounded stale snapshot only for the configured recovery interval, then returns `503`;
- logs only the first error in a consecutive refresh-failure episode.

The live Max verification used 9 windows and 149 visible panes, checked all 149 mirrored live pane identities against the installed release, served 64 concurrent viewers, consumed 6,648 KiB RSS with two threads, and left the SQLite bytes, complete tmux pane inventory, and installed semantic snapshot unchanged.

## Routes

| Route | Purpose |
|---|---|
| `/` | Homepage linking every remembered window |
| `/<window-slug>` | Direct browser view for one window |
| `/_termdeck/api/v1/snapshot` | Versioned full host snapshot JSON |
| `/_termdeck/api/v1/windows/<window-slug>` | Versioned one-window snapshot JSON |
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

- `termdeck-web-core`: dependency-free IDs, immutable snapshots, stable route allocation, and generation-safe pane leases.
- `termdeck-web-state`: strict read-only SQLite projection and optional read-only tmux inventory overlay.
- `termdeck-web-server`: current-thread Tokio/Axum HTTP server, immutable rendering cache, security middleware, and minimal browser pages.

No crate uses unsafe Rust. The production runtime has no Node.js, Electron, GTK, VTE, or Python server dependency.

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
cargo metadata --locked --no-deps --format-version 1
```

The smoke is registered in `scripts/validation_manifest.json` for both CI and production profiles.

## Deliberately not implemented yet

The following remain behind later security and parity gates:

- persistent slug storage across daemon restarts and window renames;
- owner-device enrollment and revocation;
- WebAuthn/passkeys and secure browser sessions;
- LAN TLS and Cloudflare Access validation;
- terminal byte/WebSocket streaming;
- viewer-to-controller acquisition over HTTP/WebSocket;
- canonical terminal geometry and takeover replay;
- input, paste, mouse, clipboard, history, and scrollbar behavior;
- every management, process, account, workspace, diagnostic, release, and rollback action;
- GTK/Python runtime removal and production authority cutover.

No writable SQLite or tmux path may be introduced until those contracts have failing-first coverage, adversarial review, inherited production validation, and a rehearsed rollback boundary.
