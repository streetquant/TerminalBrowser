# TermDeck web deployment

The browser client is served by a loopback-only Rust backend and a persistent nginx TLS/WebSocket edge. The edge dynamically detects the current RFC1918 address, rotates the LAN certificate under the existing private CA when the DHCP address changes, and allows only the current address, loopback, `termdeck.local`, `max.local`, and the configured Cloudflare hostname.

## Local service

Install `scripts/termdeck-web-prepare-runtime.sh` as `/8tb/projects/softwares/termdeck/.runtime/termdeck-lan/bin/termdeck-web-prepare`, install the user units from `deploy/systemd`, run `systemctl --user daemon-reload`, and enable `termdeck-web-backend.service` plus `termdeck-web-lan.service`. The user must have lingering enabled. The backend remains bound to `127.0.0.1:7391`; only nginx binds port 7443.

The certificate is signed by the existing TermDeck LAN CA and includes the current DHCP address, `127.0.0.1`, `termdeck.local`, and `max.local`. Re-running the preparation script is idempotent and rotates only when the address changes or the certificate approaches expiry.

## Cloudflare

The preferred standalone public endpoint is a dedicated named tunnel with the configuration in `deploy/cloudflare/termdeck.yml.example` and a proxied CNAME from the public hostname to `<tunnel-id>.cfargotunnel.com`. The Cloudflare token used for DNS creation must have `Zone:DNS:Edit`; tunnel-management permission alone is insufficient. Application password/device authentication, same-origin CSRF checks, sequenced terminal input and single-controller fencing remain authoritative behind the tunnel.

A stable route can also be inserted into the existing `maxchat.semiotech.org` tunnel by applying `deploy/cloudflare/maxchat-termdeck-route.json` before its hostname catch-all. That route exposes TermDeck only under `https://maxchat.semiotech.org/t1000/`, preserves the existing MaxChat root route, validates the local TLS origin through the TermDeck LAN CA, and forwards the exact `/t1000` prefix contract used by redirects, forms, assets, cookies and WSS.

When DNS permission or a stable shared route is temporarily unavailable, `deploy/systemd/termdeck-cloudflare-preview.service` and `scripts/termdeck-cloudflare-preview.py` provide a Quick Tunnel and write its current URL to `cloudflare-preview-url.json`. Quick Tunnel URLs are temporary fallbacks and must not replace the named-tunnel or stable shared route in production documentation.

Run `python3 scripts/check_termdeck_web_deployment.py` before promotion. Never commit the LAN CA private key, owner password database, tunnel credential JSON, API token, invitation secret, or generated runtime files.
