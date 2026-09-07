#!/usr/bin/bash
set -euo pipefail
umask 077

runtime=/var/lib/codexpro-exec/termdeck-runtime/termdeck-lan
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
