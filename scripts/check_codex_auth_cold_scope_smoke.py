#!/usr/bin/env python3
"""Verify a cold managed shell inherits the canonical Codex login safely."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from smoke_support import install_fixture_launcher_attestation


_LOGIN_STATUS_MARKER = "__TERMDECK_LOGIN_STATUS_RC__="


def _base64url_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _synthetic_chatgpt_auth() -> dict[str, object]:
    """Return parseable, non-secret fixture credentials for ``login status``."""

    now = int(time.time())
    token = ".".join(
        (
            _base64url_json({"alg": "RS256", "typ": "JWT", "kid": "fixture"}),
            _base64url_json(
                {
                    "iss": "https://auth.openai.com",
                    "sub": "termdeck-fixture-user",
                    "email": "termdeck-fixture@example.invalid",
                    "iat": now - 60,
                    "exp": now + 86_400,
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "termdeck-fixture-account",
                        "chatgpt_plan_type": "plus",
                        "chatgpt_user_id": "termdeck-fixture-user",
                    },
                }
            ),
            base64.urlsafe_b64encode(b"termdeck-fixture-signature")
            .decode("ascii")
            .rstrip("="),
        )
    )
    return {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-01-01T00:00:00Z",
        "tokens": {
            "access_token": token,
            "refresh_token": "termdeck-fixture-refresh-token",
            "id_token": token,
            "account_id": "termdeck-fixture-account",
        },
    }


def _version_probe_environment(home: Path) -> dict[str, str]:
    """Keep an explicitly selected binary's version probe inside its fixture."""

    paths = {
        "HOME": home,
        "CODEX_HOME": home / ".codex",
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_STATE_HOME": home / ".local" / "state",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_RUNTIME_DIR": home / "runtime",
        "TMPDIR": home / "tmp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "TZ"}
    }
    environment.update({key: str(path) for key, path in paths.items()})
    environment["PATH"] = os.defpath
    return environment


def _is_direct_codex(candidate: Path, *, probe_home: Path) -> tuple[bool, str]:
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        return False, str(exc)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return False, "not an executable file"
    if "contained" in resolved.name.casefold():
        return False, "is a contained launcher rather than the direct Codex CLI"
    try:
        with resolved.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        head = b""
    if b"termdeck-private-codex-wrapper" in head or b"codex-contained" in head:
        return False, "is a generated or contained wrapper"
    try:
        version = subprocess.run(
            [str(candidate), "--version"],
            cwd=probe_home,
            env=_version_probe_environment(probe_home),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = f"{version.stdout}\n{version.stderr}".strip()
    if version.returncode != 0 or "codex-cli" not in output.casefold():
        return False, f"unexpected --version result: {output or version.returncode}"
    return True, output


def _configured_direct_codex() -> Path | None:
    """The operator must explicitly select this qualification's real CLI."""

    configured = os.environ.get("TERMDECK_REAL_CODEX_BINARY", "").strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise ValueError("TERMDECK_REAL_CODEX_BINARY must be an explicit absolute path")
    return candidate


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        mode = oct(stat.S_IMODE(metadata.st_mode))
        if path.is_symlink():
            snapshot[relative] = f"link:{mode}:{os.readlink(path)}"
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[relative] = f"file:{mode}:{digest}"
        elif path.is_dir():
            snapshot[relative] = f"dir:{mode}"
        else:
            snapshot[relative] = f"other:{metadata.st_mode}"
    return snapshot


def _wait_for_login_status(path: Path, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    output = ""
    while time.monotonic() < deadline:
        try:
            output = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            output = ""
        if _LOGIN_STATUS_MARKER in output:
            return output
        time.sleep(0.05)
    raise AssertionError(
        "codex login status did not finish inside the managed shell; "
        f"partial output: {output[-2000:]!r}"
    )


def main() -> int:
    codex_launcher = _configured_direct_codex()
    if codex_launcher is None:
        print(
            json.dumps(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": (
                        "Real Codex qualification requires an operator-selected "
                        "TERMDECK_REAL_CODEX_BINARY absolute path; local installations "
                        "are not discovered automatically."
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    tmux_executable = shutil.which("tmux")
    with tempfile.TemporaryDirectory(prefix="td-") as tmp:
        root = Path(tmp)
        # Version checks are executions too. Probe only the selected path, in
        # a fresh home with no inherited provider credentials or user config.
        probe_home = root / "version-probe-home"
        probe_home.mkdir(mode=0o700)
        direct, codex_version = _is_direct_codex(codex_launcher, probe_home=probe_home)
        if not direct:
            raise RuntimeError(
                f"Explicit TERMDECK_REAL_CODEX_BINARY was rejected: {codex_version}"
            )
        if tmux_executable is None:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "tmux is unavailable; the managed-shell auth smoke cannot run",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        fake_user_home = root / "home"
        canonical_home = fake_user_home / ".codex"
        canonical_accounts = canonical_home / "accounts"
        auth_root = root / "terminal-auth"
        tmux_tmp = root / "tmux"
        project = root / "project"
        host_bin = root / "host-bin"
        for directory in (
            canonical_accounts,
            auth_root,
            tmux_tmp,
            project,
            host_bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        canonical_home.chmod(0o700)
        canonical_accounts.chmod(0o700)
        auth_root.chmod(0o700)
        tmux_tmp.chmod(0o700)

        canonical_credential = canonical_accounts / "alpha.json"
        canonical_credential.write_text(
            json.dumps(_synthetic_chatgpt_auth(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        canonical_credential.chmod(0o600)
        (canonical_home / "current").write_text("alpha\n", encoding="utf-8")
        (canonical_home / "current").chmod(0o600)
        (canonical_home / "auth.json").symlink_to(Path("accounts") / "alpha.json")
        source_before = _snapshot_tree(canonical_home)

        # Give the isolated shell an unambiguous `codex` command without adding
        # or invoking any of the machine's TermDeck/contained launcher wrappers.
        (host_bin / "codex").symlink_to(codex_launcher)
        host_path_entries = [
            str(host_bin),
            str(Path(tmux_executable).parent),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        os.environ["HOME"] = str(fake_user_home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(host_path_entries))
        os.environ["SHELL"] = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
        os.environ["NO_AT_BRIDGE"] = "1"
        for key in (
            "CODEX_HOME",
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "TERMDECK_AUTH_SCOPE_ID",
            "TERMDECK_CODEX_ACCOUNT_SOURCE",
            "TERMDECK_CODEX_AUTH_BINARY",
            "TERMDECK_CODEX_BINARY",
            "TERMDECK_CODEX_STATE_SOURCE",
            "TMUX",
            "TMUX_PANE",
        ):
            os.environ.pop(key, None)
        install_fixture_launcher_attestation(codex_launcher)

        # Import only after HOME is isolated. SessionStore must take its normal
        # omitted-account_source path and resolve this temp ~/.codex itself.
        from termdeck.codex_auth import read_auth_state
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import TmuxBackend

        store = SessionStore(root / "sessions.sqlite3", auth_root=auth_root)
        backend = TmuxBackend()
        record = None
        try:
            record = store.create(
                "cold-auth",
                "terminal",
                os.environ["SHELL"],
                str(project),
            )
            persisted = store.get(record.id)
            if persisted is None:
                raise AssertionError("new managed session was not persisted")
            if record.selected_account != "alpha" or persisted.selected_account != "alpha":
                raise AssertionError(
                    "cold scope did not persist the proven canonical account: "
                    f"created={record.selected_account!r}, persisted={persisted.selected_account!r}"
                )
            if record.auth_restart_required or persisted.auth_restart_required:
                raise AssertionError("cold scope incorrectly requires an auth restart")

            private_home = Path(record.codex_home)
            private_accounts = private_home / "accounts"
            private_auth = private_home / "auth.json"
            if not private_accounts.is_symlink() or not os.path.samefile(
                private_accounts, canonical_accounts
            ):
                raise AssertionError("private account catalogue is not the canonical directory")
            if not private_auth.is_symlink() or not os.path.samefile(
                private_auth, canonical_credential
            ):
                raise AssertionError("private auth.json is not the canonical selected credential")

            state = read_auth_state(private_home, account_source=store.account_source)
            if state.selected_account_key != "alpha" or not state.canonical_binding_ready:
                raise AssertionError(
                    "cold private auth state is not canonically bound to alpha: "
                    f"selected={state.selected_account_key!r}, "
                    f"ready={state.canonical_binding_ready!r}"
                )

            backend.create_or_resume(record)
            pid = backend.pane_pid(record)
            record = store.update(record.id, status="running", last_pid=pid)
            if not backend.private_auth_environment_ready(record):
                raise AssertionError("managed shell did not start with its private CODEX_HOME")

            login_output = root / "codex-login-status.txt"
            quoted_output = shlex.quote(str(login_output))
            backend.send_text(
                record,
                (
                    f"codex login status > {quoted_output} 2>&1; "
                    "_termdeck_login_rc=$?; "
                    f"printf '\\n{_LOGIN_STATUS_MARKER}%s\\n' "
                    f'"$_termdeck_login_rc" >> {quoted_output}'
                ),
                enter=True,
            )
            output = _wait_for_login_status(login_output)
            match = re.search(
                rf"^{re.escape(_LOGIN_STATUS_MARKER)}(\d+)$",
                output,
                flags=re.MULTILINE,
            )
            if match is None or int(match.group(1)) != 0:
                raise AssertionError(
                    f"codex login status failed in private scope: {output[-2000:]!r}"
                )
            if "Logged in using ChatGPT" not in output:
                raise AssertionError(f"Codex did not see the inherited login: {output[-2000:]!r}")

            source_after = _snapshot_tree(canonical_home)
            if source_after != source_before:
                raise AssertionError(
                    "cold-scope creation or login status mutated the canonical source"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": False,
                        "codex_version": codex_version,
                        "selected_account": persisted.selected_account,
                        "canonical_accounts_samefile": True,
                        "canonical_auth_samefile": True,
                        "private_environment_ready": True,
                        "login_status": "Logged in using ChatGPT",
                        "source_unchanged": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            if record is not None:
                try:
                    backend.terminate(record)
                except Exception:
                    pass
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
