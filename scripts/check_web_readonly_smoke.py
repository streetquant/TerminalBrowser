#!/usr/bin/env python3
"""Run the authoritative bounded Rust gates for TermDeck's read-only web slice."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Final

COMMAND_TIMEOUT_SECONDS: Final = 360
MAX_CAPTURE_BYTES: Final = 8 * 1024 * 1024
MAX_FRONTEND_ASSET_BYTES: Final = 64 * 1024
FRONTEND_EXPECTED_TEXT: Final = (
    "/_termdeck/api/v1/snapshot",
    "/_termdeck/api/v1/windows/",
    "textContent",
    "replaceChildren",
    "Viewer · read-only",
    "AbortController",
    "revision_text",
)
FRONTEND_FORBIDDEN_TEXT: Final = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "localStorage",
    "sessionStorage",
)
EXPECTED_PACKAGES: Final = {
    "termdeck-web-core",
    "termdeck-web-server",
    "termdeck-web-state",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded(data: bytes, *, stream: str) -> bytes:
    if len(data) > MAX_CAPTURE_BYTES:
        raise RuntimeError(
            f"{stream} exceeded {MAX_CAPTURE_BYTES} bytes: {len(data)}"
        )
    return data


def _validate_frontend_assets(
    javascript_path: Path,
    stylesheet_path: Path,
) -> dict[str, object]:
    javascript_bytes = javascript_path.read_bytes()
    stylesheet_bytes = stylesheet_path.read_bytes()
    if len(javascript_bytes) > MAX_FRONTEND_ASSET_BYTES:
        raise RuntimeError(
            f"frontend JavaScript exceeds {MAX_FRONTEND_ASSET_BYTES} bytes: "
            f"{len(javascript_bytes)}"
        )
    if len(stylesheet_bytes) > MAX_FRONTEND_ASSET_BYTES:
        raise RuntimeError(
            f"frontend stylesheet exceeds {MAX_FRONTEND_ASSET_BYTES} bytes: "
            f"{len(stylesheet_bytes)}"
        )
    try:
        javascript = javascript_bytes.decode("utf-8")
        stylesheet_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("frontend assets must be UTF-8") from exc
    missing = [value for value in FRONTEND_EXPECTED_TEXT if value not in javascript]
    forbidden = [value for value in FRONTEND_FORBIDDEN_TEXT if value in javascript]
    if missing:
        raise RuntimeError(f"frontend JavaScript is missing required contracts: {missing}")
    if forbidden:
        raise RuntimeError(f"frontend JavaScript uses forbidden browser sinks: {forbidden}")
    if "http://" in javascript or "https://" in javascript:
        raise RuntimeError("frontend JavaScript must not contain an external origin")
    return {
        "javascript_bytes": len(javascript_bytes),
        "javascript_sha256": _sha256(javascript_bytes),
        "stylesheet_bytes": len(stylesheet_bytes),
        "stylesheet_sha256": _sha256(stylesheet_bytes),
        "node_runtime_required": False,
    }


def _run(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded(exc.stdout or b"", stream="stdout")
        stderr = _bounded(exc.stderr or b"", stream="stderr")
        raise RuntimeError(
            f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {argv!r}; "
            f"stdout_tail={stdout[-2000:]!r}; stderr_tail={stderr[-2000:]!r}"
        ) from exc
    stdout = _bounded(result.stdout, stream="stdout")
    stderr = _bounded(result.stderr, stream="stderr")
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    receipt: dict[str, object] = {
        "argv": argv,
        "elapsed_ms": elapsed_ms,
        "returncode": result.returncode,
        "stdout_bytes": len(stdout),
        "stdout_sha256": _sha256(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": _sha256(stderr),
    }
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with {result.returncode}: {argv!r}; "
            f"stdout_tail={stdout[-4000:].decode(errors='replace')!r}; "
            f"stderr_tail={stderr[-4000:].decode(errors='replace')!r}"
        )
    return receipt


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    web_root = root / "web"
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is required for the TermDeck web validation gate")
    required = [
        web_root / "Cargo.toml",
        web_root / "Cargo.lock",
        web_root / "crates" / "termdeck-web-core" / "src" / "lib.rs",
        web_root / "crates" / "termdeck-web-state" / "src" / "lib.rs",
        web_root / "crates" / "termdeck-web-server" / "src" / "lib.rs",
        web_root / "crates" / "termdeck-web-server" / "assets" / "app.css",
        web_root / "crates" / "termdeck-web-server" / "assets" / "app.js",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"required TermDeck web files are missing: {missing}")
    frontend = _validate_frontend_assets(
        web_root / "crates" / "termdeck-web-server" / "assets" / "app.js",
        web_root / "crates" / "termdeck-web-server" / "assets" / "app.css",
    )

    with tempfile.TemporaryDirectory(prefix="t1000-web-validation-") as raw_temp:
        temporary = Path(raw_temp)
        environment = os.environ.copy()
        environment.update(
            {
                "CARGO_BUILD_JOBS": "2",
                "CARGO_HOME": str(temporary / "cargo-home"),
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(temporary / "target"),
                "CARGO_TERM_COLOR": "never",
                "RUSTFLAGS": "-D warnings",
            }
        )
        metadata_command = [
            cargo,
            "metadata",
            "--locked",
            "--no-deps",
            "--format-version",
            "1",
        ]
        commands = [
            [cargo, "fmt", "--all", "--", "--check"],
            [cargo, "test", "--workspace", "--locked"],
            [
                cargo,
                "clippy",
                "--workspace",
                "--all-targets",
                "--locked",
                "--",
                "-D",
                "warnings",
                "-D",
                "clippy::all",
                "-D",
                "clippy::unwrap_used",
                "-D",
                "clippy::expect_used",
            ],
            metadata_command,
        ]
        receipts = [
            _run(command, cwd=web_root, environment=environment)
            for command in commands
        ]
        node = shutil.which("node")
        if node is not None:
            receipts.append(
                _run(
                    [
                        node,
                        "--check",
                        str(
                            web_root
                            / "crates"
                            / "termdeck-web-server"
                            / "assets"
                            / "app.js"
                        ),
                    ],
                    cwd=root,
                    environment=environment,
                )
            )
            frontend["syntax_check"] = "node --check"
        else:
            frontend["syntax_check"] = "static and Rust-embedded checks only"
        metadata_result = subprocess.run(
            metadata_command,
            cwd=web_root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            text=True,
        )
        metadata = json.loads(metadata_result.stdout)
        packages = {str(item["name"]) for item in metadata["packages"]}
        if packages != EXPECTED_PACKAGES:
            raise RuntimeError(
                f"unexpected TermDeck web package set: {sorted(packages)}"
            )
        source_hashes = {
            str(path.relative_to(root)): _sha256(path.read_bytes()) for path in required
        }
        payload = {
            "schema": "t1000.web-readonly-smoke.v1",
            "ok": True,
            "packages": sorted(packages),
            "commands": receipts,
            "frontend": frontend,
            "source_sha256": source_hashes,
        }
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - smoke must emit one bounded failure
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from exc
