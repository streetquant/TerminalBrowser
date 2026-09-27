"""Exercise the deployment template through a private real HTTPS nginx edge.

Only private paths and listener addresses differ from the checked-in template.
The upstream is an inert HTTP fixture: no auth store, tmux or live service is
opened. Its request recording makes edge refusals observable before proxying.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/nginx/termdeck-web.nginx.conf"
PUBLIC_HOST = "maxchat.semiotech.org"
NGINX = Path("/usr/bin/nginx")
OPENSSL = Path("/usr/bin/openssl")


class FixtureBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        body_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(body_length)
        record = {"method": self.command, "path": self.path,
                  "headers": {key.lower(): value for key, value in self.headers.items()}}
        self.server.requests.append(record)
        prefix = self.headers.get("X-Forwarded-Prefix", "")
        if self.path == "/":
            self.send_response(303)
            self.send_header("Location", prefix + "/_termdeck/login")
            body = b""
        elif self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            accept = hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                   "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(accept).decode())
            body = b""
            self.close_connection = True
        elif self.path == "/_termdeck/native-form":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            # Match the application's policy. The template must not override
            # this with no-referrer and make native POST forms send Origin:null.
            self.send_header("Referrer-Policy", "same-origin")
            body = (f'<form method="post" action="{prefix}/_termdeck/fixture">'
                    '<button type="submit">Submit fixture</button></form>').encode()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "termdeck_session=fixture-only; Path=" + (prefix or "/"))
            self.send_header("Set-Cookie", "termdeck_csrf=fixture-only; Path=" + (prefix or "/"))
            body = json.dumps(record).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


@contextmanager
def _nginx_process(command):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, start_new_session=True)
    descriptor = os.pidfd_open(process.pid)
    try:
        yield process
    finally:
        # Signal only the child created by this fixture, never a discovered PID.
        for chosen in (signal.SIGQUIT, signal.SIGTERM, signal.SIGKILL):
            if process.poll() is not None:
                break
            signal.pidfd_send_signal(descriptor, chosen, None, 0)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                continue
        os.close(descriptor)
        assert process.poll() is not None, "disposable nginx did not exit"
        process.stderr.close()


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    if not NGINX.is_file() or not OPENSSL.is_file():
        pytest.skip("real template qualification requires nginx and openssl")
    runtime = tmp_path_factory.mktemp("nginx-edge")
    for directory in ("nginx/logs", "nginx/temp/client", "nginx/temp/proxy", "tls"):
        (runtime / directory).mkdir(parents=True, mode=0o700)
    (runtime / "nginx/allowed-hosts.map").write_text(
        "localhost 1;\n127.0.0.1 1;\ntermdeck.local 1;\n" + PUBLIC_HOST + " 1;\n")
    certificate = runtime / "tls/server-chain.crt"
    key = runtime / "tls/server.key"
    subprocess.run([str(OPENSSL), "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                    "-nodes", "-keyout", str(key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext",
                    "subjectAltName=IP:127.0.0.1,DNS:" + PUBLIC_HOST],
                   check=True, capture_output=True, timeout=20)
    key.chmod(0o600)
    backend = ThreadingHTTPServer(("127.0.0.1", 0), FixtureBackend)
    backend.daemon_threads = True
    backend.requests = []
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    try:
        port = _free_port()
        source = TEMPLATE.read_text()
        source = source.replace("/warp/projects/softwares/termdeck/.termdeck", str(runtime))
        assert source.count("listen 0.0.0.0:7443 ssl;") == 1
        source = source.replace("listen 0.0.0.0:7443 ssl;", f"listen 127.0.0.1:{port} ssl;")
        assert source.count("server 127.0.0.1:7391;") == 1
        source = source.replace("server 127.0.0.1:7391;", f"server 127.0.0.1:{backend.server_port};")
        config = runtime / "nginx/nginx.conf"
        config.write_text(source)
        command = [str(NGINX), "-p", str(runtime / "nginx") + "/", "-c", str(config)]
        subprocess.run([*command, "-t"], check=True, capture_output=True, timeout=10)
        context = ssl.create_default_context(cafile=str(certificate))
        (runtime / "bootstrap.html").write_text("disposable bootstrap fixture")
        (runtime / "tls/ca.crt").write_bytes(certificate.read_bytes())

        def request(path, *, host=PUBLIC_HOST, method="GET", headers=None, source_address="127.0.0.1"):
            connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=context,
                                                    source_address=(source_address, 0))
            try:
                connection.request(method, path, headers={"Host": host, **(headers or {})})
                response = connection.getresponse()
                body = response.read() if response.status != 101 else b""
                return SimpleNamespace(status=response.status, headers=response.getheaders(),
                                       location=response.getheader("Location"), body=body)
            finally:
                connection.close()

        with _nginx_process([*command, "-g", "daemon off;"]) as process:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("disposable nginx exited: " + process.stderr.read().decode())
                try:
                    request("/t1000")
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("disposable nginx did not listen")
            yield SimpleNamespace(request=request, port=port, requests=backend.requests,
                                  certificate=certificate)
    finally:
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("claimed_peer", (None, "127.0.0.1", "192.168.1.10"))
@pytest.mark.parametrize("path,method", (
    ("/t1000", "GET"),
    ("/t1000/", "GET"),
    ("/t1000/_termdeck/fixture", "GET"),
    ("/t1000/_termdeck/fixture", "POST"),
    ("/_termdeck/fixture", "GET"),
    ("/_termdeck/fixture", "POST"),
    ("/_termdeck/bootstrap", "GET"),
    ("/_termdeck/lan-ca.crt", "GET"),
    ("/t1000/_termdeck/ws/fixture", "GET"),
    ("/_termdeck/ws/fixture", "GET"),
))
def test_unlisted_connection_peer_is_refused_before_any_route(edge, claimed_peer, path, method):
    # Linux loopback provides a real distinct TCP peer without changing host
    # interfaces, routes or firewall state. The production peer gate is intact.
    headers = {"Origin": "https://" + PUBLIC_HOST, "Sec-Fetch-Site": "same-origin",
               "Sec-Fetch-Mode": "cors"}
    if "/ws/" in path:
        headers.update({"Sec-Fetch-Mode": "websocket", "Upgrade": "websocket",
                        "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "ZGlzcG9zYWJsZS10ZXN0IQ=="})
    if claimed_peer is not None:
        headers.update({"X-Forwarded-For": claimed_peer, "X-Real-IP": claimed_peer,
                        "CF-Connecting-IP": claimed_peer, "Forwarded": "for=" + claimed_peer})
    before = len(edge.requests)
    response = edge.request(path, method=method, headers=headers, source_address="127.0.0.2")
    assert response.status == 403
    assert response.location is None
    assert len(edge.requests) == before


def test_loopback_tunnel_peer_is_admitted_with_external_client_headers(edge):
    response = edge.request("/t1000/_termdeck/fixture", source_address="127.0.0.1", headers={
        "X-Forwarded-For": "198.51.100.23", "X-Real-IP": "198.51.100.23",
        "CF-Connecting-IP": "198.51.100.23", "Forwarded": "for=198.51.100.23"})
    assert response.status == 200
    headers = json.loads(response.body)["headers"]
    assert headers["x-forwarded-for"] == "127.0.0.1"
    assert headers["x-real-ip"] == "127.0.0.1"


@pytest.mark.parametrize("path", ("/_termdeck/bootstrap", "/_termdeck/lan-ca.crt"))
def test_loopback_peer_retains_static_bootstrap_and_ca_access(edge, path):
    response = edge.request(path, source_address="127.0.0.1")
    assert response.status == 200
    expected = (b"disposable bootstrap fixture" if path.endswith("bootstrap")
                else edge.certificate.read_bytes())
    assert response.body == expected


@pytest.mark.parametrize("authority", (PUBLIC_HOST, PUBLIC_HOST + ":443", "127.0.0.1:{port}"))
def test_exact_prefix_redirect_preserves_browser_authority(edge, authority):
    host = authority.format(port=edge.port)
    response = edge.request("/t1000", host=host)
    assert response.status == 308
    assert response.location == "/t1000/"
    original = "https://" + host + "/t1000"
    target = urljoin(original, response.location)
    assert urlsplit(target).netloc == host
    follow = edge.request(urlsplit(target).path, host=urlsplit(target).netloc)
    assert follow.status == 303
    assert follow.location == "/t1000/_termdeck/login"


@pytest.mark.parametrize("path,prefix,upstream", (
    ("/t1000/_termdeck/fixture?keep=1", "/t1000", "/_termdeck/fixture?keep=1"),
    ("/_termdeck/fixture?keep=1", "", "/_termdeck/fixture?keep=1"),
))
def test_proxy_prefix_and_secure_cookie_contract_are_preserved(edge, path, prefix, upstream):
    response = edge.request(path)
    assert response.status == 200
    record = json.loads(response.body)
    assert record["path"] == upstream
    assert record["headers"]["host"] == "127.0.0.1:7391"
    assert record["headers"]["origin"] == "http://127.0.0.1:7391"
    assert record["headers"].get("x-forwarded-prefix", "") == prefix
    assert record["headers"]["x-forwarded-proto"] == "https"
    assert record["headers"]["x-forwarded-host"] == PUBLIC_HOST
    cookies = [value.lower() for key, value in response.headers if key.lower() == "set-cookie"]
    assert len(cookies) == 2
    assert all("secure" in value and "samesite=strict" in value for value in cookies)
    assert "httponly" in next(value for value in cookies if value.startswith("termdeck_session="))
    assert all("path=" + (prefix or "/") in value for value in cookies)


@pytest.mark.parametrize("path", ("/t1000", "/t1000/", "/_termdeck/fixture"))
def test_unknown_host_is_refused_before_redirect_or_proxy(edge, path):
    before = len(edge.requests)
    response = edge.request(path, host="unrelated.invalid")
    assert response.status == 421
    assert response.location is None
    assert len(edge.requests) == before


@pytest.mark.parametrize("path", ("/t1000/_termdeck/fixture", "/_termdeck/fixture"))
@pytest.mark.parametrize("origin,site", (
    ("https://unrelated.invalid", "same-origin"),
    ("https://termdeck.local", "same-origin"),
    ("https://" + PUBLIC_HOST + ":7443", "same-origin"),
    ("http://" + PUBLIC_HOST, "same-origin"),
    ("https://" + PUBLIC_HOST + "/", "same-origin"),
    ("null", "same-origin"),
    (None, "same-origin"),
    ("https://" + PUBLIC_HOST, "cross-site"),
    ("https://" + PUBLIC_HOST, "same-site"),
    ("https://" + PUBLIC_HOST, None),
))
def test_original_browser_provenance_is_refused_before_normalization(edge, path, origin, site):
    before = len(edge.requests)
    headers = {key: value for key, value in {
        "Origin": origin, "Sec-Fetch-Site": site, "Sec-Fetch-Mode": "cors"}.items()
        if value is not None}
    response = edge.request(path, method="POST", headers=headers)
    assert response.status in (403, 421)
    assert len(edge.requests) == before


@pytest.mark.parametrize("authority", (PUBLIC_HOST, "127.0.0.1:{port}"))
@pytest.mark.parametrize("mode", ("navigate", "cors"))
def test_legitimate_mutation_metadata_is_validated_then_forwarded(edge, authority, mode):
    host = authority.format(port=edge.port)
    response = edge.request("/t1000/_termdeck/fixture", method="POST", host=host, headers={
        "Origin": "https://" + host, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": mode})
    assert response.status == 200
    record = json.loads(response.body)
    assert record["headers"]["origin"] == "http://127.0.0.1:7391"
    assert record["headers"]["sec-fetch-site"] == "same-origin"
    assert record["headers"]["sec-fetch-mode"] == mode


def test_cross_site_safe_navigation_does_not_require_origin(edge):
    response = edge.request("/t1000/", headers={"Sec-Fetch-Site": "cross-site",
                                               "Sec-Fetch-Mode": "navigate"})
    assert response.status == 303
    assert response.location == "/t1000/_termdeck/login"


@pytest.mark.parametrize("origin,site", ((None, "same-origin"), ("null", "same-origin"),
    ("https://unrelated.invalid", "same-origin"), ("https://" + PUBLIC_HOST, "cross-site")))
@pytest.mark.parametrize("path", ("/t1000/_termdeck/ws/fixture", "/_termdeck/ws/fixture"))
def test_websocket_original_provenance_is_refused_before_proxy(edge, path, origin, site):
    before = len(edge.requests)
    headers = {key: value for key, value in {
        "Origin": origin, "Sec-Fetch-Site": site, "Upgrade": "websocket",
        "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "ZGlzcG9zYWJsZS10ZXN0IQ=="}.items() if value is not None}
    response = edge.request(path, headers=headers)
    assert response.status in (403, 421)
    assert len(edge.requests) == before


@pytest.mark.parametrize("path,prefix,upstream", (
    ("/t1000/_termdeck/ws/fixture", "/t1000", "/_termdeck/ws/fixture"),
    ("/_termdeck/ws/fixture", "", "/_termdeck/ws/fixture"),
))
@pytest.mark.parametrize("fetch_site", (None, "same-origin"))
def test_websocket_upgrade_keeps_prefix_and_handshake(edge, path, prefix, upstream, fetch_site):
    headers = {
        "Origin": "https://" + PUBLIC_HOST,
        "Sec-Fetch-Mode": "websocket", "Upgrade": "websocket", "Connection": "Upgrade",
        "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "ZGlzcG9zYWJsZS10ZXN0IQ==",
        "Sec-WebSocket-Protocol": "fixture-protocol"}
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    response = edge.request(path, headers=headers)
    assert response.status == 101
    record = edge.requests[-1]
    assert record["path"] == upstream
    assert record["headers"].get("x-forwarded-prefix", "") == prefix
    assert record["headers"]["sec-websocket-protocol"] == "fixture-protocol"


@pytest.mark.parametrize("host,prefix", ((PUBLIC_HOST, "/t1000"), ("127.0.0.1", "")))
def test_native_browser_form_preserves_original_origin(edge, host, prefix):
    playwright = pytest.importorskip("playwright.sync_api")
    chromium = Path("/usr/bin/chromium")
    if not chromium.is_file():
        pytest.skip("native form qualification requires Chromium")
    public_key = subprocess.run([str(OPENSSL), "x509", "-in", str(edge.certificate),
                                 "-pubkey", "-noout"], capture_output=True, check=True).stdout
    public_der = subprocess.run([str(OPENSSL), "pkey", "-pubin", "-outform", "DER"],
                                input=public_key, capture_output=True, check=True).stdout
    pin = base64.b64encode(hashlib.sha256(public_der).digest()).decode()
    origin = f"https://{host}:{edge.port}"
    with playwright.sync_playwright() as tool:
        browser = tool.chromium.launch(executable_path=str(chromium), headless=True,
                                       args=["--no-proxy-server", "--disable-background-networking",
                                             "--host-resolver-rules=MAP " + PUBLIC_HOST + " 127.0.0.1",
                                             "--ignore-certificate-errors-spki-list=" + pin])
        try:
            page = browser.new_page()
            page.goto(origin + prefix + "/_termdeck/native-form")
            with page.expect_request(lambda item: item.method == "POST") as pending:
                with page.expect_navigation() as navigation:
                    page.get_by_role("button", name="Submit fixture").click()
            assert navigation.value.status == 200
            assert pending.value.all_headers().get("origin") == origin
            assert pending.value.all_headers().get("sec-fetch-site") == "same-origin"
        finally:
            browser.close()
