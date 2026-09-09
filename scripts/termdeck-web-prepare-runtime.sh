#!/usr/bin/bash
set -euo pipefail
umask 077

runtime=/warp/runtime/termdeck/termdeck-lan
tls="$runtime/tls"
nginx_root="$runtime/nginx"
mkdir -p "$tls" "$nginx_root/logs" "$nginx_root/temp/client" "$nginx_root/temp/proxy"
chmod 700 "$runtime" "$tls" "$nginx_root"

lan_ip=$(/usr/bin/ip -4 route get 1.1.1.1 2>/dev/null | /usr/bin/awk 'NR==1 {for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)
if [ -z "$lan_ip" ]; then
    lan_ip=$(/usr/bin/ip -4 -o address show scope global | /usr/bin/awk 'NR==1 {split($4,a,"/"); print a[1]}')
fi
/usr/bin/python3 - "$lan_ip" <<'PY'
import ipaddress,sys
address=ipaddress.ip_address(sys.argv[1])
private=(ipaddress.ip_network('10.0.0.0/8'),ipaddress.ip_network('172.16.0.0/12'),ipaddress.ip_network('192.168.0.0/16'))
if address.version != 4 or not any(address in network for network in private):
    raise SystemExit(f"TermDeck refused non-RFC1918 primary IPv4 address: {address}")
PY

hosts_tmp=$(/usr/bin/mktemp "$nginx_root/.allowed-hosts.XXXXXX")
cat >"$hosts_tmp" <<EOF_HOSTS
localhost 1;
127.0.0.1 1;
termdeck.local 1;
max.local 1;
$lan_ip 1;
maxchat.semiotech.org 1;
semiotech.org 1;
termdeck.semiotech.org 1;
EOF_HOSTS
chmod 600 "$hosts_tmp"
mv -f "$hosts_tmp" "$nginx_root/allowed-hosts.map"

ca_cert="$tls/ca.crt"
ca_key="$tls/ca.key"
cert="$tls/server.crt"
key="$tls/server.key"
chain="$tls/server-chain.crt"
for required in "$ca_cert" "$ca_key"; do
    [ -s "$required" ] || { echo "missing TermDeck LAN CA material: $required" >&2; exit 1; }
done

rotate=0
if [ ! -s "$cert" ] || [ ! -s "$key" ] || [ ! -s "$chain" ]; then
    rotate=1
elif ! /usr/bin/openssl x509 -in "$cert" -noout -checkend 2592000 >/dev/null 2>&1; then
    rotate=1
elif ! /usr/bin/openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | /usr/bin/grep -Fq "IP Address:$lan_ip"; then
    rotate=1
fi
if [ "$rotate" -eq 1 ]; then
    work=$(/usr/bin/mktemp -d "$tls/.rotate.XXXXXX")
    trap '/usr/bin/rm -rf "$work"' EXIT
    cat >"$work/server.ext" <<EOF_EXT
[v3_req]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=@alt_names
[alt_names]
DNS.1=termdeck.local
DNS.2=max.local
IP.1=$lan_ip
IP.2=127.0.0.1
EOF_EXT
    /usr/bin/openssl req -new -newkey rsa:2048 -nodes -subj '/CN=termdeck.local' \
        -keyout "$work/server.key" -out "$work/server.csr" >/dev/null 2>&1
    /usr/bin/openssl x509 -req -in "$work/server.csr" -CA "$ca_cert" -CAkey "$ca_key" \
        -CAserial "$tls/ca.srl" -days 825 -sha256 -extfile "$work/server.ext" \
        -extensions v3_req -out "$work/server.crt" >/dev/null 2>&1
    /usr/bin/openssl verify -CAfile "$ca_cert" "$work/server.crt" >/dev/null
    cat "$work/server.crt" "$ca_cert" >"$work/server-chain.crt"
    chmod 600 "$work/server.key"
    chmod 644 "$work/server.crt" "$work/server-chain.crt"
    mv -f "$work/server.key" "$key"
    mv -f "$work/server.crt" "$cert"
    mv -f "$work/server-chain.crt" "$chain"
    cp -f "$work/server.ext" "$tls/server.ext"
    trap - EXIT
    rm -rf "$work"
fi
/usr/bin/openssl verify -CAfile "$ca_cert" "$cert" >/dev/null
/usr/bin/openssl x509 -in "$cert" -noout -ext subjectAltName | /usr/bin/grep -Fq "IP Address:$lan_ip"
printf '%s\n' "$lan_ip" >"$runtime/current-lan-ip"
chmod 600 "$runtime/current-lan-ip" "$nginx_root/allowed-hosts.map"

# Keep the LAN runtime receipt derived from the exact files that the service
# will use.  The receipt is written last, after TLS rotation and host-map
# generation, so an atomic update can never describe a partial runtime.
/usr/bin/python3 - "$runtime" "$lan_ip" <<'PY'
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

runtime = Path(sys.argv[1]).resolve()
lan_ip_object = ipaddress.ip_address(sys.argv[2])
lan_ip = str(lan_ip_object)
if lan_ip_object.version != 4 or not lan_ip_object.is_private:
    raise SystemExit(f"TermDeck receipt refused non-private IPv4 address: {lan_ip}")

config = runtime / "nginx" / "nginx.conf"
listen = re.findall(r"^\s*listen\s+[^;:]+:(\d+)\s+ssl\s*;\s*$", config.read_text(encoding="utf-8"), re.MULTILINE)
if len(listen) != 1:
    raise SystemExit("TermDeck receipt requires exactly one TLS listen port")
https_port = int(listen[0])
if not 1 <= https_port <= 65535:
    raise SystemExit(f"TermDeck receipt found invalid HTTPS port: {https_port}")

relative_files = (
    "bin/termdeck-web-server",
    "nginx/nginx.conf",
    "tls/ca.crt",
    "tls/server-chain.crt",
    "tls/server.crt",
)
files: dict[str, str] = {}
for relative in relative_files:
    path = runtime / relative
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"TermDeck receipt requires a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    files[relative] = digest.hexdigest()

receipt = {
    "files": files,
    "https_port": https_port,
    "lan_ip": lan_ip,
    "ok": True,
    "schema": "termdeck.lan-runtime-build.v1",
}
destination = runtime / "build-receipt.json"
fd, temporary = tempfile.mkstemp(prefix=".build-receipt.", dir=runtime)
temporary_path = Path(temporary)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary_path, 0o644)
    os.replace(temporary_path, destination)
    directory_fd = os.open(runtime, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if temporary_path.exists():
        temporary_path.unlink()
PY
