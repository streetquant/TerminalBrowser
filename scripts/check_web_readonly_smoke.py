#!/usr/bin/env python3
"""Run bounded source, terminal, authentication, and build gates for TermDeck web."""

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
MAX_TERMINAL_ASSET_BYTES: Final = 1024 * 1024
MAX_INTERACTIVE_SOURCE_BYTES: Final = 1536 * 1024
MAX_FRONTEND_BUILD_SOURCE_BYTES: Final = 256 * 1024
MAX_AUTH_SOURCE_FILE_BYTES: Final = 256 * 1024
MAX_AUTH_SOURCE_TOTAL_BYTES: Final = 768 * 1024
MAX_AUTH_HTTP_SOURCE_BYTES: Final = 512 * 1024
EXPECTED_PACKAGES: Final = {
    "termdeck-web-auth",
    "termdeck-web-core",
    "termdeck-web-server",
    "termdeck-web-state",
}
EXPECTED_AUTH_DEPENDENCIES: Final = {
    "argon2",
    "getrandom",
    "rusqlite",
    "sha2",
    "termdeck-web-core",
    "zeroize",
}
FRONTEND_EXPECTED_TEXT: Final = (
    "/_termdeck/api/v1/snapshot",
    "/_termdeck/api/v1/windows/",
    "/_termdeck/ws/v1/windows/",
    "termdeck.terminal.v1",
    "new WebSocket",
    "terminal-xterm",
    "workspace-headerbar",
    "workspace-tab-close",
    "New tab",
    "replaceChildren",
    "textContent",
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
    "Take control",
    "Take over control",
    "Release control",
    "terminal-composer",
    "/control/release",
    "/input/text",
    "/input/key",
)
TERMINAL_SOURCE_EXPECTED_TEXT: Final = (
    "termdeck.terminal.v1",
    "WebSocketUpgrade",
    "refresh-client -A",
    "capture-pane -epq",
    "%extended-output",
    "send-keys -t",
    "InputDecision::Duplicate",
    "InputOutcome::Indeterminate",
    "MAX_TERMINAL_INPUT_BYTES",
    "MAX_CONTROL_BODY_BYTES",
    "MAX_REPLAY_BYTES",
    "TabActionService",
    'OsString::from("new")',
    'OsString::from("rename")',
    'Self::Hide => "hide"',
    'Self::Kill => "kill"',
)
TERMINAL_SOURCE_FORBIDDEN_TEXT: Final = (
    "bash -c",
    "sh -c",
    'Command::new("bash")',
    'Command::new("sh")',
    'std::process::Command::new("bash")',
    "eval(",
    "shell=True",
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
    "OwnerPasswordStore",
    "OWNER_PASSWORD_FILENAME",
    "Algorithm::Argon2id",
    "configure_once",
    "create_password_session",
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
AUTH_HTTP_EXPECTED_TEXT: Final = (
    "--allow-insecure-loopback-auth",
    "termdeck_session",
    "termdeck_csrf",
    "SameSite=Strict",
    "HttpOnly",
    "x-termdeck-csrf",
    "sec-fetch-site",
    "AuthStore::open_or_create",
    "redeem_invitation",
    "verify_csrf",
    "revoke_session",
    "revoke_device",
    "PAIRING_PATH",
    "SETUP_PATH",
    "LOGIN_PATH",
    "setup_password",
    "login_password",
    "MAX_PASSWORD_OPERATIONS",
    "SESSION_PATH",
    "LOGOUT_PATH",
    "DEVICES_PATH",
    "NonLoopbackRequiresAuthentication",
)
AUTH_HTTP_FORBIDDEN_PRODUCTION_TEXT: Final = (
    '"send-keys"',
    '"new-session"',
    '"split-window"',
    '"kill-pane"',
    '"capture-pane"',
    "ControllerRegistry",
    "ControllerLease",
    "Command::new",
    "tokio::process",
    "rusqlite::",
    "ReadOnlyStateMirror",
    "TmuxInventory",
)
EXPECTED_AUTH_CONSUMERS: Final = ["termdeck-web-server"]


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
    terminal_javascript_path: Path,
    terminal_stylesheet_path: Path,
    terminal_manifest_path: Path,
    frontend_package_path: Path,
    frontend_lock_path: Path,
    frontend_build_path: Path,
    terminal_entry_path: Path,
    notices_path: Path,
) -> dict[str, object]:
    app_javascript_bytes = javascript_path.read_bytes()
    app_stylesheet_bytes = stylesheet_path.read_bytes()
    terminal_javascript_bytes = terminal_javascript_path.read_bytes()
    terminal_stylesheet_bytes = terminal_stylesheet_path.read_bytes()
    for label, data in (
        ("application JavaScript", app_javascript_bytes),
        ("application stylesheet", app_stylesheet_bytes),
    ):
        if len(data) > MAX_FRONTEND_ASSET_BYTES:
            raise RuntimeError(
                f"frontend {label} exceeds {MAX_FRONTEND_ASSET_BYTES} bytes: {len(data)}"
            )
    for label, data in (
        ("terminal JavaScript", terminal_javascript_bytes),
        ("terminal stylesheet", terminal_stylesheet_bytes),
    ):
        if len(data) > MAX_TERMINAL_ASSET_BYTES:
            raise RuntimeError(
                f"frontend {label} exceeds {MAX_TERMINAL_ASSET_BYTES} bytes: {len(data)}"
            )
    try:
        javascript = app_javascript_bytes.decode("utf-8")
        app_stylesheet_bytes.decode("utf-8")
        terminal_javascript = terminal_javascript_bytes.decode("utf-8")
        terminal_stylesheet_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("frontend assets must be UTF-8") from exc
    missing = [value for value in FRONTEND_EXPECTED_TEXT if value not in javascript]
    forbidden = [value for value in FRONTEND_FORBIDDEN_TEXT if value in javascript]
    if missing:
        raise RuntimeError(f"frontend JavaScript is missing required contracts: {missing}")
    if forbidden:
        raise RuntimeError(f"frontend JavaScript uses forbidden or obsolete contracts: {forbidden}")
    if "http://" in javascript or "https://" in javascript:
        raise RuntimeError("application JavaScript must not contain an external origin")
    if "TermDeckTerminal" not in terminal_javascript or "termdeck-xterm-v1" not in terminal_javascript:
        raise RuntimeError("terminal bundle is missing its bounded global wrapper")
    if "Bundled license information" not in terminal_javascript:
        raise RuntimeError("terminal bundle is missing bundled license information")

    manifest = json.loads(terminal_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "termdeck.frontend-assets.v1":
        raise RuntimeError("terminal asset manifest schema is invalid")
    expected_packages = {
        "@xterm/addon-fit": "0.11.0",
        "@xterm/addon-search": "0.16.0",
        "@xterm/addon-web-links": "0.12.0",
        "@xterm/addon-webgl": "0.19.0",
        "@xterm/xterm": "6.0.0",
    }
    if manifest.get("packages") != expected_packages:
        raise RuntimeError(f"terminal package set changed: {manifest.get('packages')!r}")
    if manifest.get("builder") != "esbuild@0.28.2":
        raise RuntimeError("terminal builder identity changed")
    outputs = manifest.get("outputs", {})
    for name, data in (
        ("terminal.js", terminal_javascript_bytes),
        ("terminal.css", terminal_stylesheet_bytes),
    ):
        observed = outputs.get(name)
        expected = {"bytes": len(data), "sha256": _sha256(data)}
        if observed != expected:
            raise RuntimeError(
                f"terminal asset manifest mismatch for {name}: observed={observed!r} expected={expected!r}"
            )

    package = json.loads(frontend_package_path.read_text(encoding="utf-8"))
    if package.get("private") is not True or package.get("type") != "module":
        raise RuntimeError("frontend build package must remain private ESM")
    if package.get("dependencies") != expected_packages:
        raise RuntimeError("frontend dependency pins changed")
    if package.get("devDependencies") != {"esbuild": "0.28.2"}:
        raise RuntimeError("frontend build dependency pin changed")
    lock = json.loads(frontend_lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 3:
        raise RuntimeError("frontend lockfile version changed")
    locked_root = lock.get("packages", {}).get("", {})
    if locked_root.get("dependencies") != expected_packages:
        raise RuntimeError("frontend lock root dependency pins changed")
    if locked_root.get("devDependencies") != {"esbuild": "0.28.2"}:
        raise RuntimeError("frontend lock root builder pin changed")
    required_locked = {
        "node_modules/@xterm/xterm": "6.0.0",
        "node_modules/@xterm/addon-fit": "0.11.0",
        "node_modules/@xterm/addon-webgl": "0.19.0",
        "node_modules/@xterm/addon-search": "0.16.0",
        "node_modules/@xterm/addon-web-links": "0.12.0",
        "node_modules/esbuild": "0.28.2",
    }
    for name, version in required_locked.items():
        entry = lock.get("packages", {}).get(name)
        if not isinstance(entry, dict) or entry.get("version") != version or not entry.get("integrity"):
            raise RuntimeError(f"frontend lock entry is missing or unverified: {name}")

    build_sources = {
        frontend_build_path: frontend_build_path.read_bytes(),
        terminal_entry_path: terminal_entry_path.read_bytes(),
    }
    total_build_source = sum(len(value) for value in build_sources.values())
    if total_build_source > MAX_FRONTEND_BUILD_SOURCE_BYTES:
        raise RuntimeError(
            f"frontend build source exceeds {MAX_FRONTEND_BUILD_SOURCE_BYTES} bytes: {total_build_source}"
        )
    build_text = b"\n".join(build_sources.values()).decode("utf-8")
    if "platform: \"browser\"" not in build_text or "sourcemap: false" not in build_text:
        raise RuntimeError("frontend build no longer produces a bounded browser-only bundle")
    if "@xterm/xterm/css/xterm.css" not in build_text:
        raise RuntimeError("terminal source no longer bundles the xterm stylesheet")
    notices = notices_path.read_text(encoding="utf-8")
    for package, version in {**expected_packages, "esbuild": "0.28.2"}.items():
        if f"`{package}` {version}" not in notices:
            raise RuntimeError(f"third-party notice is missing {package} {version}")
    if "Permission is hereby granted" not in notices or "THE SOFTWARE IS PROVIDED" not in notices:
        raise RuntimeError("third-party notice is missing the MIT license terms")

    return {
        "application_javascript_bytes": len(app_javascript_bytes),
        "application_javascript_sha256": _sha256(app_javascript_bytes),
        "application_stylesheet_bytes": len(app_stylesheet_bytes),
        "application_stylesheet_sha256": _sha256(app_stylesheet_bytes),
        "terminal_javascript_bytes": len(terminal_javascript_bytes),
        "terminal_javascript_sha256": _sha256(terminal_javascript_bytes),
        "terminal_stylesheet_bytes": len(terminal_stylesheet_bytes),
        "terminal_stylesheet_sha256": _sha256(terminal_stylesheet_bytes),
        "terminal_packages": expected_packages,
        "build_source_sha256": {
            str(path): _sha256(data) for path, data in build_sources.items()
        },
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
    production_parts: list[str] = []
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
        production_parts.append(source.split("#[cfg(test)]", 1)[0])
    if total > MAX_AUTH_SOURCE_TOTAL_BYTES:
        raise RuntimeError(
            f"authentication source exceeds {MAX_AUTH_SOURCE_TOTAL_BYTES} total bytes: {total}"
        )
    complete = "\n".join(source_parts)
    production = "\n".join(production_parts)
    missing = [value for value in AUTH_EXPECTED_TEXT if value not in complete]
    forbidden = [value for value in AUTH_FORBIDDEN_PRODUCTION_TEXT if value in production]
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


def _validate_authenticated_http(server_root: Path) -> dict[str, object]:
    manifest_path = server_root / "Cargo.toml"
    library_path = server_root / "src" / "lib.rs"
    auth_path = server_root / "src" / "auth_http.rs"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    dependencies = set(manifest.get("dependencies", {}))
    if "termdeck-web-auth" not in dependencies:
        raise RuntimeError("web server does not declare the authentication foundation")
    library_bytes = library_path.read_bytes()
    auth_bytes = auth_path.read_bytes()
    total = len(library_bytes) + len(auth_bytes)
    if total > MAX_AUTH_HTTP_SOURCE_BYTES:
        raise RuntimeError(
            f"authenticated HTTP source exceeds {MAX_AUTH_HTTP_SOURCE_BYTES} bytes: {total}"
        )
    try:
        library = library_bytes.decode("utf-8")
        auth_source = auth_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("authenticated HTTP source must be UTF-8") from exc
    combined = library + "\n" + auth_source
    missing = [value for value in AUTH_HTTP_EXPECTED_TEXT if value not in combined]
    production_auth = auth_source.split("#[cfg(test)]", 1)[0]
    forbidden = [
        value for value in AUTH_HTTP_FORBIDDEN_PRODUCTION_TEXT if value in production_auth
    ]
    if missing:
        raise RuntimeError(f"authenticated HTTP contracts are missing: {missing}")
    if forbidden:
        raise RuntimeError(
            "authenticated HTTP acquired forbidden terminal or database authority: "
            f"{forbidden}"
        )
    if "if !config.listen.ip().is_loopback()" not in library:
        raise RuntimeError("authenticated HTTP no longer refuses non-loopback binding")
    return {
        "enabled_by_default": False,
        "explicit_insecure_loopback_opt_in": True,
        "non_loopback_authority": False,
        "auth_module_controller_authority": False,
        "auth_module_terminal_authority": False,
        "auth_module_tmux_authority": False,
        "auth_module_existing_registry_authority": False,
        "source_bytes": total,
        "source_sha256": {
            str(library_path.relative_to(server_root.parents[2])): _sha256(library_bytes),
            str(auth_path.relative_to(server_root.parents[2])): _sha256(auth_bytes),
        },
    }


def _validate_interactive_terminal(server_root: Path) -> dict[str, object]:
    source_paths = [
        server_root / "src" / "lib.rs",
        server_root / "src" / "terminal_http.rs",
        server_root / "src" / "terminal_stream.rs",
        server_root / "src" / "tab_actions.rs",
    ]
    source_bytes = {path: path.read_bytes() for path in source_paths}
    total = sum(len(value) for value in source_bytes.values())
    if total > MAX_INTERACTIVE_SOURCE_BYTES:
        raise RuntimeError(
            f"interactive terminal source exceeds {MAX_INTERACTIVE_SOURCE_BYTES} bytes: {total}"
        )
    try:
        sources = [value.decode("utf-8") for value in source_bytes.values()]
    except UnicodeDecodeError as exc:
        raise RuntimeError("interactive terminal source must be UTF-8") from exc
    source = "\n".join(sources)
    production = "\n".join(value.split("#[cfg(test)]", 1)[0] for value in sources)
    missing = [value for value in TERMINAL_SOURCE_EXPECTED_TEXT if value not in source]
    forbidden = [value for value in TERMINAL_SOURCE_FORBIDDEN_TEXT if value in production]
    if missing:
        raise RuntimeError(f"interactive terminal contracts are missing: {missing}")
    if forbidden:
        raise RuntimeError(
            f"interactive terminal source uses forbidden shell interpolation: {forbidden}"
        )
    if 'Message::Binary' not in source or 'Message::Text' not in source:
        raise RuntimeError("terminal stream no longer distinguishes byte and control frames")
    if 'InputDecision::Duplicate' not in source or 'last_receipt' not in source:
        raise RuntimeError("terminal input no longer has exact duplicate evidence")
    if 'confirmation != Some(session_id)' not in source:
        raise RuntimeError("destructive tab kill no longer requires exact identity confirmation")
    if 'try_acquire_owned()' not in source:
        raise RuntimeError("terminal/action operation bounds are missing")
    return {
        "authenticated_websocket": True,
        "tmux_control_mode": True,
        "exact_byte_output": True,
        "automatic_controller": True,
        "manual_controller_ui": False,
        "input_receipts": "monotonic-exact-duplicate-only",
        "shell_interpolation": False,
        "tab_actions": ["new", "rename", "pause", "resume", "pin", "unpin", "hide", "kill"],
        "source_bytes": total,
        "source_sha256": {
            str(path.relative_to(server_root.parents[2])): _sha256(data)
            for path, data in source_bytes.items()
        },
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    web_root = root / "web"
    auth_root = web_root / "crates" / "termdeck-web-auth"
    server_root = web_root / "crates" / "termdeck-web-server"
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
        auth_root / "src" / "password.rs",
        auth_root / "src" / "store.rs",
        web_root / "crates" / "termdeck-web-core" / "src" / "lib.rs",
        web_root / "crates" / "termdeck-web-state" / "src" / "lib.rs",
        server_root / "Cargo.toml",
        server_root / "src" / "lib.rs",
        server_root / "src" / "auth_http.rs",
        server_root / "src" / "terminal_http.rs",
        server_root / "src" / "terminal_stream.rs",
        server_root / "src" / "tab_actions.rs",
        server_root / "assets" / "app.css",
        server_root / "assets" / "app.js",
        server_root / "assets" / "terminal.css",
        server_root / "assets" / "terminal.js",
        server_root / "assets" / "terminal-assets.json",
        web_root / "frontend" / "package.json",
        web_root / "frontend" / "package-lock.json",
        web_root / "frontend" / "build.mjs",
        web_root / "frontend" / "src" / "terminal-entry.js",
        web_root / "THIRD_PARTY_NOTICES.md",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"required TermDeck web files are missing: {missing}")

    frontend = _validate_frontend_assets(
        server_root / "assets" / "app.js",
        server_root / "assets" / "app.css",
        server_root / "assets" / "terminal.js",
        server_root / "assets" / "terminal.css",
        server_root / "assets" / "terminal-assets.json",
        web_root / "frontend" / "package.json",
        web_root / "frontend" / "package-lock.json",
        web_root / "frontend" / "build.mjs",
        web_root / "frontend" / "src" / "terminal-entry.js",
        web_root / "THIRD_PARTY_NOTICES.md",
    )
    authentication = _validate_auth_foundation(auth_root)
    authenticated_http = _validate_authenticated_http(server_root)
    interactive_terminal = _validate_interactive_terminal(server_root)

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
            for asset in (server_root / "assets" / "app.js", server_root / "assets" / "terminal.js"):
                receipts.append(
                    _run(
                        [node, "--check", str(asset)],
                        cwd=root,
                        environment=environment,
                    )
                )
            frontend["syntax_check"] = "node --check app.js and terminal.js"
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
        if consumers != EXPECTED_AUTH_CONSUMERS:
            raise RuntimeError(
                "authentication consumer boundary changed: "
                f"observed={consumers} expected={EXPECTED_AUTH_CONSUMERS}"
            )
        authentication["library_only"] = True
        authentication["integrated_consumers"] = consumers

        payload = {
            "schema": "t1000.web-full-client-smoke.v5",
            "ok": True,
            "packages": sorted(packages),
            "commands": receipts,
            "authentication": authentication,
            "authenticated_http": authenticated_http,
            "interactive_terminal": interactive_terminal,
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
