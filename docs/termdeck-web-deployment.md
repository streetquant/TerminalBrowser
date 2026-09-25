# t1000 web deployment

The browser client is served by a loopback-only Rust backend and a persistent nginx TLS/WebSocket edge. The edge dynamically detects the current RFC1918 address, rotates the LAN certificate under the existing private CA when the DHCP address changes, and allows only the current address, loopback, `termdeck.local`, `max.local`, and the configured Cloudflare hostname.

## Local service

Install `scripts/termdeck-web-prepare-runtime.sh` as `/warp/projects/softwares/termdeck/.termdeck/bin/termdeck-web-prepare`, install the user units from `deploy/systemd`, run `systemctl --user daemon-reload`, and enable `termdeck-web-backend.service` plus `termdeck-web-lan.service`. The user must have lingering enabled. The backend remains bound to `127.0.0.1:7391`; only nginx binds port 7443.

The certificate is signed by the existing t1000 LAN CA and includes the current DHCP address, `127.0.0.1`, `termdeck.local`, and `max.local`. Re-running the preparation script is idempotent and rotates only when the address changes or the certificate approaches expiry.

## Canonical tmux runtime

The managed backend wrapper admits `.termdeck/state/tmux` through the production
storage contract before executing the web server. Desktop/CLI tmux commands use
the same admitted directory. `TMUX_TMPDIR` is a persistent pathname, never a
`/proc/.../fd` address. The wrapper carries its observed device, inode and owner
into the web process; inventory, capture, input and WebSocket commands recheck
that identity and use the explicit `tmux-<uid>/default` socket. Inherited `TMUX`
values are removed and cannot select another server. Managed invocations never
fall back to `/tmp`.

The runtime and socket parent must be direct, owner-only directories. Socket
permissions must match tmux's native private-socket modes; the enclosing parent
remains mode 0700. Symlinks, owner/device/identity drift, unsafe modes and foreign
socket peers are rejected. A live peer must be the expected tmux executable,
owned by the runtime user, with the admitted `TMUX_TMPDIR`. Validation does not
repair unsafe permissions or delete an unknown socket.

A missing socket permits the backend to start and display saved metadata, but
terminal operations report unavailability. Discovery of a path is not session
recovery. Creating an empty server is not recovery either. Only explicit
session creation/recovery may start sessions; the guarded recovery workflow must
independently validate saved identity, working directory and recipe. Preserve
the session database and private authentication scopes while that workflow is
pending. The activation watchdog described below restores deployment
transactions; it does not recreate user terminals.

For disposable browser qualification, run
`python scripts/check_web_terminal_browser.py --server-binary web/target/debug/termdeck-web-server --managed-tmux --output <private-evidence-directory>`.
This exercises admitted path/peer routing across the launcher exec boundary,
inventory, HTTP input, WebSocket output, window/tab actions and backend restart.
Its temporary storage is a test fixture, not production storage attestation or
proof that the public endpoint has been upgraded.

Registry reconciliation uses a fresh pane inventory. When the server is visible
in the same PID namespace, a live process must have the tmux server as its parent
and retain its observed kernel start time through sampling. Query failures are
reported as unavailable; they do not prove absence. Missing/dead panes may
become recovery-pending only with a valid authorized recipe. A transaction
compares the complete observed registry row before applying status changes, so
a concurrent stop, move or account change wins. Scope metadata is retained and
reported as present, missing, partial, unsafe or unavailable without reading
credentials. Repeated reconciliation does not recreate sessions or erase
working directories, recipes or authentication scopes.

## Activation recovery evidence

The web activation helper stores transaction receipts and rollback markers by default under `/warp/projects/softwares/termdeck/.termdeck/state/recovery/activation-v2`. This state-root recovery hierarchy is separate from the t1000 runtime and session-state directories. Historical recovery artifacts under `/var/lib/cloudmcp-exec` are evidence only; use an explicit reviewed `--evidence` path when replaying a historical transaction.

`termdeck-web-recovery.service` is the activation watchdog. Its qualified
template starts the release-bound managed wrapper, which resolves the immutable
release's `termdeck.web_activation` module with Python isolated mode. It watches
`LATEST` under the canonical evidence directory. No pending transaction is an
inactive condition; terminal transactions exit successfully. A stale heartbeat
permits rollback only after acquiring the activation lock and revalidating the
exact transaction, release generation, unit, wrapper and publication identity.
Rollback preserves the retained release and transaction evidence for retry.
Publish this unit through the activation workflow alongside the wrappers; do
not install a checkout-importing watchdog or treat its installation as terminal
recovery. The disposable activation tests cover interrupted publication,
stale-worker rejection, A/B rollback and repeated frozen-watchdog execution.

## Cloudflare

The preferred standalone public endpoint is a dedicated named tunnel with the configuration in `deploy/cloudflare/termdeck.yml.example` and a proxied CNAME from the public hostname to `<tunnel-id>.cfargotunnel.com`. The Cloudflare token used for DNS creation must have `Zone:DNS:Edit`; tunnel-management permission alone is insufficient. Application password/device authentication, same-origin CSRF checks, sequenced terminal input and single-controller fencing remain authoritative behind the tunnel.

A stable route can also be inserted into the existing `maxchat.semiotech.org` tunnel by applying `deploy/cloudflare/maxchat-termdeck-route.json` before its hostname catch-all. That route exposes t1000 only under `https://maxchat.semiotech.org/t1000/`, preserves the existing MaxChat root route, validates the local TLS origin through the t1000 LAN CA, and forwards the exact `/t1000` prefix contract used by redirects, forms, assets, cookies and WSS.

When DNS permission or a stable shared route is temporarily unavailable, `deploy/systemd/termdeck-cloudflare-preview.service` and `scripts/termdeck-cloudflare-preview.py` provide a Quick Tunnel and write its current URL to `cloudflare-preview-url.json`. Quick Tunnel URLs are temporary fallbacks and must not replace the named-tunnel or stable shared route in production documentation.

Run `python3 scripts/check_termdeck_web_deployment.py` before promotion. Never commit the LAN CA private key, owner password database, tunnel credential JSON, API token, invitation secret, or generated runtime files.
