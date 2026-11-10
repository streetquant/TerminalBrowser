# TermDeck behind Cloudflare

How the public TermDeck endpoint is hosted, what sits behind it, and what must not
change. Evidence was captured on 2026-11-11 against the live host; commands are in
[Evidence](#evidence).

Status: **public entry live** — `https://maxchat.semiotech.org/t1000/` answers
`303` to the TermDeck login, served through the shared `maxchat` tunnel.

## Data path

```
browser
  │  HTTPS  https://maxchat.semiotech.org/t1000/...
  ▼
Cloudflare edge            universal SSL for *.semiotech.org, cf-ray …-SIN
  │  tunnel: maxchat (23cb5dd6-…)
  │  ingress rule: path ^/t1000(?:/.*)?$  →  https://127.0.0.1:7443
  ▼
nginx 0.0.0.0:7443         t1000 LAN CA certificate, server_name _
  │  strips /t1000, sets X-Forwarded-Prefix: /t1000
  ▼
termdeck-web-server        loopback only, 127.0.0.1:7391
  │
  ├── session registry .termdeck/state/sessions.sqlite3
  └── tmux server      .termdeck/state/tmux/tmux-1000/default
```

The backend never binds a public interface. Only nginx listens on `7443`, and only
nginx is reachable from the tunnel. LAN clients use the same nginx on the current
LAN address with the private CA.

## Tunnels on this account

| tunnel | UUID | ingress | connector |
|---|---|---|---|
| **maxchat** | `23cb5dd6-a69e-4f55-8e5e-8392c85d19d3` | `maxchat.semiotech.org` (path `/t1000`) → `https://127.0.0.1:7443` | active; connector `a3fe90d3-…`, cloudflared 2026.10.2, origin IP `103.176.11.11` |
| termdeck | `57fb93cb-7f04-40c2-b825-9b4807fb9697` | `termdeck.semiotech.org` → `https://127.0.0.1:7443` | **none**; `termdeck-cloudflared.service` installed but inactive |
| semiotech-files | `99a248e5-41a1-4701-9a9e-8ee5ec3006fd` | `semiotech.org` → `http://192.168.68.60:8081` | `cloudflared-semiotech.service` (user `max`, `~/.cloudflared/config.yml`) |
| cloudmcp | token-managed | unrelated service | `cloudflared.service` (root, `/etc/cloudflared/cloudmcp.token`) |

The public TermDeck route is carried by the **maxchat** tunnel, not by the
dedicated `termdeck` tunnel. The dedicated tunnel and its unit exist so the
service can be moved off the shared hostname without a code change; they are not
currently connected.

The maxchat connector is not one of this host's two local connectors and its
ingress is not rendered from a local file. Its rule is maintained out of band
(Cloudflare dashboard / remote-managed configuration). The canonical copy of that
rule lives in the repository at `deploy/cloudflare/maxchat-termdeck-route.json`;
treat that file as the source of truth and diff it against the dashboard when
the public route misbehaves.

## Ingress rule (ordering is load-bearing)

`deploy/cloudflare/maxchat-termdeck-route.json`:

```json
{
  "hostname": "maxchat.semiotech.org",
  "path": "^/t1000(?:/.*)?$",
  "service": "https://127.0.0.1:7443",
  "originRequest": {
    "originServerName": "termdeck.local",
    "caPool": "/warp/projects/softwares/termdeck/.termdeck/tls/ca.crt",
    "httpHostHeader": "maxchat.semiotech.org",
    "connectTimeout": "5s",
    "tlsTimeout": "5s",
    "tcpKeepAlive": "30s",
    "keepAliveConnections": 100,
    "keepAliveTimeout": "90s"
  }
}
```

`maxchat.semiotech.org` is shared with other applications. **This rule must
precede the hostname's MaxChat catch-all.** If it moves below the catch-all, the
public `/t1000` path silently starts answering from the wrong origin. Preserve
every rule after it as well.

`originServerName: termdeck.local` and `caPool: .termdeck/tls/ca.crt` are both
required: the connector reaches nginx over TLS and must validate the private-CA
certificate by its internal name, not by the public hostname.

## Origin: nginx on 7443

Rendered from `deploy/nginx/termdeck-web.nginx.conf` into
`.termdeck/nginx/nginx.conf` (mode `0600`, owner `max`), started by the managed
wrapper `termdeck-web-lan` under `termdeck-web-lan.service`.

- **TLS**: `.termdeck/tls/server-chain.crt` + `server.key`, issued by the t1000
  LAN CA. SANs: `termdeck.local`, `max.local`, `192.168.68.60`, `127.0.0.1`.
  Certificate lifetime 2026-10-14 → 2029-01-16. TLS 1.2/1.3, tickets off,
  session cache shared.
- **Prefix contract**: `location = /t1000` returns `308 /t1000/`;
  `location ^~ /t1000/` rewrites the prefix away before proxying and sets
  `X-Forwarded-Prefix: /t1000`, so redirects, forms, assets, APIs and WSS URLs
  stay under the public hostname. The browser keeps its own authority; it is
  never handed nginx's private origin port.
- **WebSocket**: `location ^~ /_termdeck/ws/` upgrades with
  `proxy_read_timeout 86400s`, buffering off, and sets the session cookie
  `Secure; HttpOnly; SameSite=Strict`.
- **Backend**: `proxy_pass http://termdeck_backend` → `127.0.0.1:7391`, with
  `Host` and `Origin` rewritten to the loopback origin and
  `Sec-Fetch-Site: same-origin`.

Two consequences worth remembering:

- The `/t1000/` prefix is trusted only when the request arrives with the public
  hostname (`X-Forwarded-Host: maxchat.semiotech.org`, set by nginx from `$host`).
  A LAN client that forces `/t1000/...` against `192.168.68.60:7443` gets
  `403 {"error":"request-forbidden"}` from the backend, by design. LAN and
  internal clients use the unprefixed path.
- The public hostname is **not** a SAN on the origin certificate, so TLS to the
  origin must use SNI `termdeck.local` — which is exactly what
  `originServerName: termdeck.local` in the tunnel rule provides. Pointing
  `originServerName` at `maxchat.semiotech.org` fails certificate verification.

### Peer and host admission

Two independent gates, both in nginx:

```nginx
geo $remote_addr $termdeck_origin_peer_allowed {
    default 0;
    127.0.0.1/32 1;        # the tunnel connector
    192.168.68.0/22 1;     # current LAN
}
map $host $termdeck_host_allowed { include .../allowed-hosts.map; }
```

`allowed-hosts.map` currently admits `localhost`, `127.0.0.1`, `termdeck.local`,
`max.local`, `192.168.68.60`, `maxchat.semiotech.org`, `semiotech.org`,
`termdeck.semiotech.org`. Forwarded client-IP headers cannot grant admission; the
decision is made on the socket peer. A LAN subnet change requires updating and
re-qualifying this allowlist.

## Authentication

TermDeck login only. There is **no Cloudflare Access application** in front of
`maxchat.semiotech.org/t1000`: the public entry answers `303` to
`/t1000/_termdeck/login`, and no response contains a `cloudflareaccess.com`
redirect. Password/device authentication, same-origin CSRF checks, sequenced
terminal input and single-controller fencing stay authoritative behind the
tunnel. Do not add an Access login without changing the client contract
(cookies, redirects and WSS would need to be re-qualified).

Universal SSL on `*.semiotech.org` provides the public certificate. That is a
dashboard setting (`SSL/TLS → Edge Certificates`); a successful handshake proves
current trust, not that the setting is enabled or will renew.

## Runtime units and ordering

| unit | role |
|---|---|
| `termdeck-web-backend.service` | `termdeck-web-server` on `127.0.0.1:7391` |
| `termdeck-web-lan.service` | nginx on `0.0.0.0:7443`; `After=` and `Wants=` the backend |
| `termdeck-cloudflared.service` | dedicated `termdeck` tunnel; inactive today |
| `termdeck-cloudflare-preview.service` | disposable Quick Tunnel (see below) |

`termdeck-cloudflared.service` runs
`cloudflared --config /home/arch/.cloudflared/termdeck.yml --no-autoupdate tunnel run`
and is ordered after `termdeck-web-lan.service`. Enabling it publishes
`termdeck.semiotech.org` through the dedicated tunnel; it does not affect the
`maxchat` route.

### Quick Tunnel preview

`scripts/termdeck-cloudflare-preview.py` (run by
`termdeck-cloudflare-preview.service`) starts a `trycloudflare.com` Quick Tunnel
against the local edge using an intentionally empty config, so it can never pick
up a named tunnel's credentials. It writes the URL to
`.termdeck/cloudflare-preview-url.json` and tears the tunnel down on exit. This
is a diagnostic surface only: Quick Tunnel URLs are unauthenticated by design and
must not be used to serve real sessions.

## Operating it

```sh
# public entry (expect 303 to /t1000/_termdeck/login)
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://maxchat.semiotech.org/t1000/

# local edge, unprefixed LAN contract (expect 200 login page)
curl -s --cacert .termdeck/tls/ca.crt -o /dev/null -w '%{http_code}\n' \
  --resolve 192.168.68.60:7443:192.168.68.60 \
  https://192.168.68.60:7443/_termdeck/login

# same over the internal name
curl -s --cacert .termdeck/tls/ca.crt -o /dev/null -w '%{http_code}\n' \
  --resolve termdeck.local:7443:127.0.0.1 https://termdeck.local:7443/_termdeck/login

# tunnel state
cloudflared tunnel list
cloudflared tunnel info 23cb5dd6-a69e-4f55-8e5e-8392c85d19d3

# units
systemctl --user is-active termdeck-web-backend.service termdeck-web-lan.service
systemctl --user show termdeck-web-lan.service -p MainPID -p NRestarts -p ActiveState

# certificate
openssl x509 -in .termdeck/tls/server.crt -noout -dates -ext subjectAltName
```

`cloudflared tunnel info` reporting **no active connection** for `maxchat` means
the public route is down even though the local units are healthy.

## Failure modes

| symptom | likely cause | check |
|---|---|---|
| public 502/1033 | connector down or ingress rule missing/moved | `cloudflared tunnel info 23cb5dd6-…`; compare dashboard rule with `deploy/cloudflare/maxchat-termdeck-route.json` |
| public 403 | origin peer or host rejected | nginx `geo` block and `.termdeck/nginx/allowed-hosts.map` |
| public 404 for everything | rule ordered after the MaxChat catch-all | dashboard rule order |
| TLS error at the origin | `originServerName` / `caPool` mismatch, or cert rotated without restarting the edge | `openssl x509` on `.termdeck/tls/server.crt`; nginx error log |
| login loop at `/t1000` | prefix stripped or `X-Forwarded-Prefix` missing | `location ^~ /t1000/` block |
| LAN works, public does not | tunnel layer only | tunnel status vs local `curl --cacert` |

## Invariants

1. The `/t1000` path rule stays before the hostname's catch-all.
2. `originServerName: termdeck.local` and `caPool: …/.termdeck/tls/ca.crt`.
3. The public prefix contract (`308` for `/t1000`, strip + `X-Forwarded-Prefix`
   for `/t1000/`) stays intact: cookies, CSRF origins and WSS depend on it.
4. Peer admission stays `127.0.0.1/32` + the current LAN; headers never decide it.
5. No Cloudflare Access application on this hostname unless the client contract is
   re-qualified.
6. The backend stays loopback-only; only nginx binds `7443`.

## Repository artefacts

| path | purpose |
|---|---|
| `deploy/cloudflare/maxchat-termdeck-route.json` | canonical ingress rule to insert in the shared hostname tunnel |
| `deploy/cloudflare/termdeck.yml.example` | dedicated tunnel config template |
| `deploy/nginx/termdeck-web.nginx.conf` | origin edge source |
| `scripts/termdeck-cloudflare-preview.py` | disposable Quick Tunnel helper |
| `.termdeck/cloudflare-stable-url.json` (runtime, not tracked) | stable URL record: mode `maxchat-prefixed-tunnel`, route `^/t1000(?:/.*)?$`, tunnel `23cb5dd6-…` |

## Known gaps

- The host that runs the **maxchat** connector is not identified by local state:
  the connector reports origin IP `103.176.11.11` (this host's egress) but no
  local cloudflared process serves that tunnel, so its ingress is remote-managed.
  Confirm the connector host in the Cloudflare dashboard before changing the rule.
- The dedicated `termdeck` tunnel (`57fb93cb-…`) has no active connector; the
  service is provisioned but not enabled.
- Local cloudflared is `2026.10.2`; upstream recommends `2026.10.7`.
- Universal SSL enablement and renewal are dashboard facts, not verifiable from
  this host.

## Evidence

Captured 2026-11-11 on the production host:

```
$ curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://maxchat.semiotech.org/t1000/
303 https://maxchat.semiotech.org/t1000/_termdeck/login

$ curl -s -D - https://maxchat.semiotech.org/t1000/_termdeck/login | head
HTTP/2 200
server: cloudflare
cf-cache-status: DYNAMIC
cf-ray: a587b109faade428-SIN
strict-transport-security: max-age=31536000
content-security-policy: default-src 'none'; …
(no cloudflareaccess marker in headers or body)

$ cloudflared tunnel list
maxchat   23cb5dd6-a69e-4f55-8e5e-8392c85d19d3  2 connections
termdeck  57fb93cb-7f04-40c2-b825-9b4807fb9697  no connections

$ systemctl --user is-active termdeck-web-backend.service termdeck-web-lan.service
active
active

$ openssl x509 -in .termdeck/tls/server.crt -noout -dates -ext subjectAltName
notBefore=Oct 14 00:56:05 2026 GMT
notAfter=Jan 16 00:56:05 2029 GMT
X509v3 Subject Alternative Name:
    DNS:termdeck.local, DNS:max.local, IP Address:192.168.68.60, IP Address:127.0.0.1
```

See also: [termdeck-web-deployment.md](termdeck-web-deployment.md) for the
backend/activation contract and the activation transaction rules.
