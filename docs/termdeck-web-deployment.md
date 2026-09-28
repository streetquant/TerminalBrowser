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

### Existing tmux server lifecycle

A first server can start in a delegated `t1000-tmux-server-*.scope` when the
user manager is available. `systemd-run --scope` executes tmux locally and
inherits the caller's environment and mount namespace, so private socket
directories do not require visibility from a manager-launched service.
Already-running servers in a verified dedicated scope or process-only anchor
remain accepted. If scope
creation is unavailable, explicit session creation keeps its existing direct
start behavior; that does not prove the server can survive its owner's exit.

Legacy servers in an unverified lifecycle are left in their original cgroup.
Admission returns `ok=false`, `changed=false`, the observed server identity,
and a `recovery-blocked` diagnostic. It creates no anchor, moves no process,
stops no unit, and does not create a replacement server. GUI exit remains
blocked when stopping its service could kill those sessions.

The local API audit examined systemd 262. `AttachProcessesToUnit` accepts
`ssau` (unit, subgroup, numeric PIDs), as recorded in
`/usr/share/dbus-1/interfaces/org.freedesktop.systemd1.Manager.xml`. Scope
creation gained the transient `PIDFDs` property (`ah`) in systemd 255, recorded
in `/usr/share/doc/systemd/NEWS`. The installed implementation retains that
process reference, but `unit_attach_pids_to_cgroup` calls `pidref_verify` and
then passes a numeric PID to `cg_attach`, which writes `cgroup.procs`.
Consequently, neither a PIDFD transport nor a systemd version alone establishes
a generation-bound cgroup mutation. Automatic legacy migration requires an
operation that binds the process generation through the actual membership
change; no such operation is qualified by this implementation.

There is no migration rollback on refusal because admission changes nothing.
Keep the existing owning service running. Clearing this blocker requires
separately authorized lifecycle recovery that preserves the sessions, or their
deliberate retirement before a future server starts in a dedicated scope.
Source tests and the API inspection do not qualify that external recovery.

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

### Copied pre-wrapper installation

The historical release `c9d6204ee6dbe01dc4225970cb41014e759d38146bef824b5fbda71cb3ed707c`
used version 0.2.21 before the managed wrapper module existed. Its supported
transition verifies the complete published wheel/build identity and the copied
bootstrap, launcher, state and absent wrapper set. A missing module in any
other modern release remains an error.

Prepare a **new** transaction in the canonical recovery directory, including
the historical paths as explicit rendering inputs:

```sh
python scripts/termdeck-web-activate.py --prepare \
  --target /warp/projects/softwares/termdeck \
  --evidence /warp/projects/softwares/termdeck/.termdeck/state/recovery/activation-v2 \
  --migrate-from-root /warp/runtime/termdeck/t1000-managed \
  --migrate-from-bin-dir /warp/runtime/termdeck/bin
```

The historical directories are never read or written. The request is retained
in the prepared receipt; execute/rollback consume that exact request. Review
the resulting artifacts and complete the installed preparer/attestor gates
before using `TERMDECK_ACTIVATION_APPROVED=1 ... --execute --transaction PATH`.
Custom evidence roots are rejected by execute before database snapshots or
publication journals. A qualification-directory candidate must be prepared
again in the canonical directory; do not edit or move its frozen receipt.

Before selecting the new release, this transition stages a separate verified
frozen recovery capsule within the transaction and arms its independent
watchdog. Its loader verifies and captures Python bytes before importing them;
neither the mutable checkout nor historical A provides the recovery code. The
capsule and all release trees remain retained after success or failure.

Rollback uses the same transaction (`... --rollback --transaction PATH`). It
restores the original package bytes/modes, launcher path binding and wrapper
absence through exact journal-owned inode transitions. This intentionally
restores the original copied layout, including its retired-path limitation;
it does not silently fabricate a repaired historical A. The retained capsule
can also run `python -I -B PATH/legacy-recovery.py --rollback` independently of
the checkout. Saved sessions, authentication and unrelated services are outside
this path migration. Source/disposable tests do not prove host activation or
live rollback acceptance.

Activation waits for each service's expected executable and recorded process
identity. A systemd `active` state alone is insufficient: the LAN wrapper may
still be preparing its signed runtime before it executes nginx.
On stop, nginx drains upgraded WebSockets for at most five seconds. The
managed LAN stop wrapper targets the recorded nginx generation by pidfd,
allows ten seconds for graceful exit, then gives the same generation five
seconds to terminate. These bounds stay below the unit's 30-second stop
deadline. A failed `systemctl stop` is an activation failure even if systemd
subsequently reports the unit as inactive or failed; the transaction retains
its rollback evidence for review.

An application update can publish a new runtime receipt while retaining the
existing TLS generation. In that case rollback verifies the retained A/B
receipts and owned output identities, then asks the installed preparer to sign
a fresh runtime over the restored A inputs. It requires unchanged producer TLS
and records the new consumer file identities. The older TLS inverse is not the
inverse of this application update. Interrupted and completed attempts retain
their transaction evidence; unexpected source, TLS or output changes stop
recovery for review.

### Executable publication and interrupted preparation

Publication journals store immutable file payloads once and append compact
state records on the same journal inode. Limits are 16 MiB per file, 32 MiB of
decoded preimage/postimage data and 64 MiB for the complete journal. Capacity
is checked before publication; an oversized transaction must fail without
replacing its destinations. Existing journals remain readable for recovery.
Restart recovery removes an incomplete trailing frame only from the exact
authenticated journal inode, preserving every complete frame and immutable
payload. A compensated inverse checkpoints its restored identity before its
journal is retired, so a later retry can establish ownership again.

If journal initialization stops before its identity is bound to the activation
receipt, rollback may leave the original destination in place only after
checking its complete recorded identity, metadata and content again. An
unbound journal is retained untouched. A replacement inode, changed metadata,
or an unowned postimage requires investigation; do not delete the journal or
edit the receipt to make recovery proceed.

## Cloudflare

The selected public endpoint is `https://maxchat.semiotech.org/t1000/`, using
the existing shared tunnel. Its TermDeck path rule precedes the hostname's
MaxChat catch-all. Preserve that ordering and the unrelated root route when
reviewing `deploy/cloudflare/maxchat-termdeck-route.json`. Application
password/device authentication, same-origin CSRF checks, sequenced terminal
input and single-controller fencing remain authoritative behind the tunnel.
Use this public hostname on LAN and remote clients that need publicly trusted
HTTPS. The raw LAN address `192.168.1.10:7443` uses TermDeck's private origin
certificate and is not covered by the public hostname certificate. A website
cannot silently install or trust that private CA on another computer.

The selected access policy uses TermDeck login only. An additional Cloudflare
Access login is not configured for this hostname. Invitations, device approval,
session expiry and revocation remain controlled by TermDeck; preserve unrelated
Cloudflare Access applications on other hostnames.

The qualified origin peer allowlist admits `127.0.0.1` for the tunnel and
`192.168.1.0/24` for the current LAN. nginx checks the socket peer before
redirecting or forwarding requests; forwarded client-IP headers cannot grant
admission. A LAN subnet change requires updating and qualifying this allowlist.
This restricts HTTP and terminal control, while port 7443 still listens for
LAN TLS connections. It does not prove that outside clients cannot reach the
TCP port or that a router cannot translate an external source into a LAN peer;
retain the separate router and outside-LAN validation gates.

To check managed edge TLS, select `semiotech.org` in Cloudflare and open
**SSL/TLS → Edge Certificates**. A **Disable Universal SSL** action means the
setting is enabled; leave it enabled. Check that a Universal certificate is
Active and covers the selected hostname, normally through `*.semiotech.org`.
A successful public TLS handshake proves current trust, not that dashboard
setting or a future renewal. Keep any denied settings API read explicit in
the evidence rather than inferring renewal from the certificate issuer.

The route verifies local origin TLS using the t1000 LAN CA and supplies the
`/t1000` prefix contract for redirects, forms, assets, cookies and WSS. nginx
returns a relative `/t1000/` redirect for the entry without a slash. The browser
retains its original HTTPS authority, including an explicit LAN port, instead
of receiving nginx's private origin port at the public hostname.

Before translating headers for the loopback backend, nginx checks any supplied
Origin against the original HTTPS Host, including its port. Mutations require
Origin and same-origin Fetch Metadata. WebSocket upgrades require Origin and
reject contradictory Fetch Metadata; native clients may omit Fetch Metadata.
Safe navigation may omit Origin. The proxied response's `same-origin` referrer
policy preserves native POST form provenance without sending referrers across
origins. Backend authentication, CSRF and rate limits still apply.

Qualify changes to this edge with
`python -m pytest tests/test_web_nginx_edge.py`: it runs the actual deployment
template with private paths, loopback listeners, a disposable certificate and
an inert upstream. It checks public/LAN redirects, rejected hosts/origins,
prefix/cookie/WebSocket forwarding and native Chromium forms. This fixture
does not prove live authentication or external terminal access. The nginx
template is an application activation input, not a privileged preparer package
member; publish it through qualified activation and fresh signed preparation,
never by editing the receipt-bound live config.

The standalone configuration in `deploy/cloudflare/termdeck.yml.example` is an
alternative for an independently approved hostname; it is not a replacement
for the selected shared route. Creating DNS records requires appropriate DNS
permission, which tunnel-management permission alone does not establish.

When DNS permission or a stable shared route is temporarily unavailable, `deploy/systemd/termdeck-cloudflare-preview.service` and `scripts/termdeck-cloudflare-preview.py` provide a Quick Tunnel and write its current URL to `cloudflare-preview-url.json`. Quick Tunnel URLs are temporary fallbacks and must not replace the named-tunnel or stable shared route in production documentation.

Run `python3 scripts/check_termdeck_web_deployment.py` before promotion. Never commit the LAN CA private key, owner password database, tunnel credential JSON, API token, invitation secret, or generated runtime files.
