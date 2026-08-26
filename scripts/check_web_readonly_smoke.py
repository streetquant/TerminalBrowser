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
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"required TermDeck web files are missing: {missing}")

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
            [cargo, "metadata", "--locked", "--no-deps", "--format-version", "1"],
        ]
        receipts = [
            _run(command, cwd=web_root, environment=environment)
            for command in commands
        ]
        metadata_result = subprocess.run(
            commands[-1],
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
