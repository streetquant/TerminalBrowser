#!/usr/bin/python3
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import signal
import subprocess
import sys
import time

RUNTIME = Path("/warp/runtime/termdeck/termdeck-lan")
URL_FILE = RUNTIME / "cloudflare-preview-url.json"
EMPTY_CONFIG = RUNTIME / "cloudflare-preview-empty.yml"
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
child: subprocess.Popen[str] | None = None


def write_url(url: str) -> None:
    payload = {
        "schema": "termdeck.cloudflare-preview-url.v1",
        "url": url,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cloudflared_pid": child.pid if child is not None else None,
    }
    temporary = URL_FILE.with_name(URL_FILE.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, URL_FILE)


def stop(signum: int, _frame: object) -> None:
    if child is not None and child.poll() is None:
        child.send_signal(signal.SIGTERM)


def main() -> int:
    global child
    URL_FILE.unlink(missing_ok=True)
    EMPTY_CONFIG.write_text("# Intentionally empty: isolate Quick Tunnel from named configs.\n")
    os.chmod(EMPTY_CONFIG, 0o600)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    command = [
        "/usr/bin/cloudflared",
        "--config",
        str(EMPTY_CONFIG),
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        "127.0.0.1:20344",
        "--loglevel",
        "info",
        "--transport-loglevel",
        "warn",
        "--url",
        "https://127.0.0.1:7443",
        "--http-host-header",
        "termdeck.semiotech.org",
        "--origin-server-name",
        "termdeck.local",
        "--origin-ca-pool",
        str(RUNTIME / "tls/ca.crt"),
        "--proxy-connect-timeout",
        "5s",
        "--proxy-tls-timeout",
        "5s",
        "--proxy-tcp-keepalive",
        "30s",
        "--proxy-keepalive-connections",
        "100",
        "--proxy-keepalive-timeout",
        "90s",
    ]
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert child.stdout is not None
    for line in child.stdout:
        print(line, end="", flush=True)
        match = URL_PATTERN.search(line)
        if match:
            write_url(match.group(0))
    return int(child.wait())


if __name__ == "__main__":
    raise SystemExit(main())
