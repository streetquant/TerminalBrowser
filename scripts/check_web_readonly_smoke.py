#!/usr/bin/env python3
"""Run bounded source and build gates for TermDeck's read-only web/auth slices."""

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
import tomllib
from typing import Final

COMMAND_TIMEOUT_SECONDS: Final = 600
MAX_CAPTURE_BYTES: Final = 8 * 1024 * 1024
MAX_FRONTEND_ASSET_BYTES: Final = 64 * 1024
MAX_AUTH_SOURCE_FILE_BYTES: Final = 256 * 1024
MAX_AUTH_SOURCE_TOTAL_BYTES: Final = 768 * 1024
EXPECTED_PACKAGES: Final = {
    "termdeck-web-auth",
    "termdeck-web-core",
    "termdeck-web-server",
    "termdeck-web-state",
}
EXPECTED_AUTH_DEPENDENCIES: Final = {
    "getrandom",
    "rusqlite",
    "sha2",
    "termdeck-web-core",
    "zeroize",
}
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
AUTH_EXPECTED_TEXT: Final = (
    "#![forbid(unsafe_code)]",
    "getrandom::fill",
    '.field("value", &"[REDACTED]")',
    "rate_subject_digest",
    "SQLITE_OPEN_NOFOLLOW",
    "TransactionBehavior::Immediate",
    "MAX_AUTH_DATABASE_BYTES",
    "HARD_MAX_AUDIT_EVENTS",
    "host_id",
    "prune_expired_auth_state",
    "configure_database_size_bound",
    "validate_strict_tables",
    "validate_table_definitions",
    "validate_trigger_definitions",
    "validate_foreign_keys",
    "validate_stored_row_bounds",
    "validate_stored_identifiers",
    "verify_audit_chain_connection",
    "unexpected auth views",
    "BoundedRateLimiter",
)
AUTH_FORBIDDEN_PRODUCTION_TEXT: Final = (
    "SQLITE_OPEN_CREATE",
    "std::process",
    "tokio::process",
    "Command::new",
    "std::net",
    "tokio::net",
    "axum::",
    "hyper::",
    "ReadOnlyStateMirror",
    "TmuxInventory",
    '"list-panes"',
    '"send-keys"',
    '"new-session"',
    '"capture-pane"',
    "sessions.sqlite3",
    "unsafe {",
    "unsafe fn",
)


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
    receipt: dict[str, object] = {
        "argv": argv,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
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


def _validate_auth_foundation(auth_root: Path) -> dict[str, object]:
    manifest_path = auth_root / "Cargo.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("package", {}).get("name") != "termdeck-web-auth":
        raise RuntimeError("authentication crate package identity is invalid")
    dependencies = set(manifest.get("dependencies", {}))
    if dependencies != EXPECTED_AUTH_DEPENDENCIES:
        raise RuntimeError(
            "authentication dependency boundary changed: "
            f"observed={sorted(dependencies)} "
            f"expected={sorted(EXPECTED_AUTH_DEPENDENCIES)}"
        )
    if "bin" in manifest or "build-dependencies" in manifest:
        raise RuntimeError("authentication foundation must remain library-only")

    source_paths = sorted((auth_root / "src").glob("*.rs"))
    if not source_paths:
        raise RuntimeError("authentication foundation source is missing")
    source_bytes: dict[Path, bytes] = {}
    source_parts: list[str] = []
    total = 0
    for path in source_paths:
        data = path.read_bytes()
        if len(data) > MAX_AUTH_SOURCE_FILE_BYTES:
            raise RuntimeError(
                f"authentication source exceeds {MAX_AUTH_SOURCE_FILE_BYTES} bytes: {path}"
            )
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"authentication source must be UTF-8: {path}") from exc
        total += len(data)
        source_bytes[path] = data
        source_parts.append(source)
    if total > MAX_AUTH_SOURCE_TOTAL_BYTES:
        raise RuntimeError(
            f"authentication source exceeds {MAX_AUTH_SOURCE_TOTAL_BYTES} total bytes: {total}"
        )
    complete = "\n".join(source_parts)
    missing = [value for value in AUTH_EXPECTED_TEXT if value not in complete]
    forbidden = [value for value in AUTH_FORBIDDEN_PRODUCTION_TEXT if value in complete]
    if missing:
        raise RuntimeError(f"authentication contracts are missing: {missing}")
    if forbidden:
        raise RuntimeError(
            f"authentication foundation acquired forbidden runtime authority: {forbidden}"
        )
    return {
        "crate": "termdeck-web-auth",
        "dependencies": sorted(dependencies),
        "source_bytes": total,
        "source_sha256": {
            str(path.relative_to(auth_root.parents[2])): _sha256(data)
            for path, data in source_bytes.items()
        },
        "http_authority": False,
        "tmux_authority": False,
        "process_authority": False,
        "existing_registry_authority": False,
        "raw_capabilities_persisted": False,
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    web_root = root / "web"
    auth_root = web_root / "crates" / "termdeck-web-auth"
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is required for the TermDeck web validation gate")

    required = [
        web_root / "Cargo.toml",
        web_root / "Cargo.lock",
        auth_root / "Cargo.toml",
        auth_root / "src" / "lib.rs",
        auth_root / "src" / "secret.rs",
        auth_root / "src" / "rate_limit.rs",
        auth_root / "src" / "store.rs",
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
    authentication = _validate_auth_foundation(auth_root)

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
            [cargo, "tree", "--workspace", "--locked"],
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
        auth_packages = [
            item for item in metadata["packages"] if item["name"] == "termdeck-web-auth"
        ]
        if len(auth_packages) != 1:
            raise RuntimeError("TermDeck authentication package metadata is ambiguous")
        auth_package = auth_packages[0]
        auth_dependencies = {
            str(item["name"]) for item in auth_package.get("dependencies", [])
        }
        if auth_dependencies != EXPECTED_AUTH_DEPENDENCIES:
            raise RuntimeError(
                "unexpected TermDeck authentication dependency set: "
                f"{sorted(auth_dependencies)}"
            )
        target_kinds = [list(target["kind"]) for target in auth_package.get("targets", [])]
        if target_kinds != [["lib"]]:
            raise RuntimeError(
                f"authentication package must remain library-only: {target_kinds}"
            )
        consumers = sorted(
            str(item["name"])
            for item in metadata["packages"]
            if item["name"] != "termdeck-web-auth"
            and any(
                dependency["name"] == "termdeck-web-auth"
                for dependency in item.get("dependencies", [])
            )
        )
        if consumers:
            raise RuntimeError(
                "authentication foundation was integrated before its HTTP gate: "
                f"{consumers}"
            )
        authentication["library_only"] = True
        authentication["integrated_consumers"] = consumers

        payload = {
            "schema": "t1000.web-readonly-smoke.v2",
            "ok": True,
            "packages": sorted(packages),
            "commands": receipts,
            "authentication": authentication,
            "frontend": frontend,
            "source_sha256": {
                str(path.relative_to(root)): _sha256(path.read_bytes())
                for path in required
            },
        }
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - smoke emits one bounded failure
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from exc
