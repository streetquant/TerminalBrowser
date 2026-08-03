from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SessionRecord


class CodexAuthError(RuntimeError):
    """Raised when a private Codex account operation cannot be completed."""


@dataclass(frozen=True)
class CodexAccount:
    account_key: str
    email: str = ""
    alias: str = ""
    account_name: str = ""
    active: bool = False

    @property
    def label(self) -> str:
        return self.alias or self.account_name or self.email or self.account_key

    @property
    def switch_selector(self) -> str:
        # Stable codex-auth releases reliably accept aliases and emails. The
        # opaque account key remains a final fallback for newer releases.
        return self.alias or self.email or self.account_key

    def to_dict(self) -> dict[str, object]:
        return {
            "account_key": self.account_key,
            "email": self.email,
            "alias": self.alias,
            "account_name": self.account_name,
            "label": self.label,
            "active": self.active,
        }


@dataclass(frozen=True)
class CodexAuthState:
    codex_home: str
    accounts: tuple[CodexAccount, ...]
    selected_account: str | None
    selected_account_key: str | None
    fingerprint: str
    codex_auth_available: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "codex_home": self.codex_home,
            "selected_account": self.selected_account,
            "selected_account_key": self.selected_account_key,
            "fingerprint": self.fingerprint,
            "codex_auth_available": self.codex_auth_available,
            "accounts": [account.to_dict() for account in self.accounts],
        }


@dataclass(frozen=True)
class CodexSwitchResult:
    ok: bool
    state: CodexAuthState
    error: str = ""


def default_account_source() -> Path:
    configured = os.environ.get("TERMDECK_CODEX_ACCOUNT_SOURCE", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def new_auth_scope_id() -> str:
    return f"auth-{uuid.uuid4().hex}"


def _secure_permissions(root: Path) -> None:
    if not root.exists():
        return
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        try:
            current_path.chmod(0o700)
        except OSError:
            pass
        for directory in directories:
            try:
                (current_path / directory).chmod(0o700)
            except OSError:
                pass
        for filename in files:
            try:
                (current_path / filename).chmod(0o600)
            except OSError:
                pass


def _write_private_text(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = None
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    if current != content:
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o700 if executable else 0o600)
        temporary.replace(path)
    path.chmod(0o700 if executable else 0o600)


def _clear_registry_active_account(registry_path: Path) -> None:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return
    if not isinstance(payload, dict):
        return
    changed = False
    for key in (
        "active_account_key",
        "previous_active_account_key",
        "active_account_activated_at_ms",
        "active_email",
        "activeEmail",
    ):
        if payload.get(key) is not None:
            payload[key] = None
            changed = True
    if changed:
        _write_private_text(
            registry_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )


def _is_termdeck_wrapper(candidate: Path) -> bool:
    try:
        if not candidate.is_file():
            return False
        with candidate.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return b".termdeck-compat-home" in head and b"export CODEX_HOME=" in head


def _sanitized_host_path(codex_home: Path) -> str:
    private_bin = (codex_home / "bin").resolve(strict=False)
    clean: list[str] = []
    for raw_entry in os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        entry_path = Path(entry).expanduser().resolve(strict=False)
        if entry_path == private_bin:
            continue
        if _is_termdeck_wrapper(entry_path / "codex-auth"):
            continue
        clean.append(str(entry_path))
    return os.pathsep.join(dict.fromkeys(clean)) or "/usr/local/bin:/usr/bin:/bin"


def _real_codex_auth(codex_home: Path) -> Path | None:
    configured = os.environ.get("TERMDECK_CODEX_AUTH_BINARY", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve(strict=False)
        if candidate.is_file() and os.access(candidate, os.X_OK) and not _is_termdeck_wrapper(candidate):
            return candidate
    for entry in _sanitized_host_path(codex_home).split(os.pathsep):
        candidate = Path(entry) / "codex-auth"
        if candidate.is_file() and os.access(candidate, os.X_OK) and not _is_termdeck_wrapper(candidate):
            return candidate.resolve(strict=False)
    return None


def _compat_wrapper(codex_home: Path) -> str:
    compat_home = codex_home / ".termdeck-compat-home"
    host_path = _sanitized_host_path(codex_home)
    executable = _real_codex_auth(codex_home)
    lines = [
        "#!/bin/sh",
        "set -eu",
        f"export CODEX_HOME={shlex.quote(str(codex_home))}",
        f"export HOME={shlex.quote(str(compat_home))}",
        f"export PATH={shlex.quote(host_path)}",
    ]
    if executable is not None:
        lines.append(f'exec {shlex.quote(str(executable))} "$@"')
    else:
        lines.append('exec codex-auth "$@"')
    lines.append("")
    return "\n".join(lines)


def prepare_auth_scope(
    codex_home: str | Path,
    *,
    account_source: str | Path | None = None,
) -> Path:
    """Create one durable, anonymous Codex home and compatibility wrapper.

    The saved account catalogue is copied once, but the source ``auth.json`` is
    deliberately never copied. Stable codex-auth versions that hard-code
    ``~/.codex`` are isolated by running them with a private compatibility HOME.
    """

    home = Path(codex_home).expanduser().resolve(strict=False)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)

    accounts = home / "accounts"
    source = Path(account_source).expanduser() if account_source else default_account_source()
    source_accounts = source / "accounts"
    if not accounts.exists() and source_accounts.is_dir():
        shutil.copytree(source_accounts, accounts, symlinks=False)
        _secure_permissions(accounts)
        _clear_registry_active_account(accounts / "registry.json")

    compat_home = home / ".termdeck-compat-home"
    compat_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    compat_home.chmod(0o700)
    compat_codex = compat_home / ".codex"
    if compat_codex.is_symlink():
        if compat_codex.resolve(strict=False) != home:
            compat_codex.unlink()
    elif compat_codex.exists():
        raise CodexAuthError(f"private codex-auth compatibility path is not a symlink: {compat_codex}")
    if not compat_codex.exists():
        compat_codex.symlink_to(home, target_is_directory=True)

    _write_private_text(home / "bin" / "codex-auth", _compat_wrapper(home), executable=True)
    return home


def _registry_accounts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    raw_accounts = payload.get("accounts", [])
    if isinstance(raw_accounts, dict):
        accounts = []
        for key, value in raw_accounts.items():
            row = dict(value) if isinstance(value, dict) else {}
            row.setdefault("account_key", str(key))
            accounts.append(row)
    elif isinstance(raw_accounts, list):
        accounts = [row for row in raw_accounts if isinstance(row, dict)]
    else:
        accounts = []
    active_key = payload.get("active_account_key")
    if isinstance(active_key, str) and active_key.strip():
        return accounts, active_key.strip()
    active_email = payload.get("active_email") or payload.get("activeEmail")
    if isinstance(active_email, str) and active_email.strip():
        for row in accounts:
            if str(row.get("email", "")).casefold() == active_email.strip().casefold():
                key = str(row.get("account_key") or row.get("key") or active_email).strip()
                return accounts, key or None
    return accounts, None


def read_auth_state(codex_home: str | Path) -> CodexAuthState:
    home = Path(codex_home).expanduser().resolve(strict=False)
    registry_path = home / "accounts" / "registry.json"
    payload: dict[str, Any] = {}
    registry_bytes = b""
    try:
        registry_bytes = registry_path.read_bytes()
        parsed = json.loads(registry_bytes.decode("utf-8"))
        if isinstance(parsed, dict):
            payload = parsed
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        pass

    rows, active_key = _registry_accounts(payload)
    accounts: list[CodexAccount] = []
    for index, row in enumerate(rows):
        key = str(row.get("account_key") or row.get("key") or row.get("email") or f"account-{index + 1}").strip()
        if not key:
            continue
        accounts.append(
            CodexAccount(
                account_key=key,
                email=str(row.get("email") or "").strip(),
                alias=str(row.get("alias") or "").strip(),
                account_name=str(row.get("account_name") or row.get("name") or "").strip(),
                active=key == active_key,
            )
        )
    active = next((account for account in accounts if account.active), None)
    auth_path = home / "auth.json"
    fingerprint = hashlib.sha256(
        registry_bytes
        + b"\0"
        + (b"auth-present" if auth_path.is_file() else b"auth-absent")
    ).hexdigest()
    return CodexAuthState(
        codex_home=str(home),
        accounts=tuple(accounts),
        selected_account=active.label if active else None,
        selected_account_key=active.account_key if active else None,
        fingerprint=fingerprint,
        codex_auth_available=_real_codex_auth(home) is not None,
    )


def resolve_account(state: CodexAuthState, query: str) -> CodexAccount:
    needle = query.strip().casefold()
    if not needle:
        raise CodexAuthError("account selector cannot be empty")
    matches = []
    for account in state.accounts:
        candidates = {
            account.account_key,
            account.email,
            account.alias,
            account.account_name,
            account.label,
        }
        if any(candidate and candidate.casefold() == needle for candidate in candidates):
            matches.append(account)
    if not matches:
        raise CodexAuthError(f"saved Codex account not found: {query}")
    unique = {account.account_key: account for account in matches}
    if len(unique) != 1:
        raise CodexAuthError(f"Codex account selector is ambiguous: {query}")
    return next(iter(unique.values()))


class CodexAuthManager:
    def __init__(self, *, account_source: str | Path | None = None, timeout: float = 30.0):
        self.account_source = Path(account_source).expanduser() if account_source else default_account_source()
        self.timeout = max(1.0, float(timeout))

    def state(self, record: SessionRecord) -> CodexAuthState:
        if not record.codex_home:
            raise CodexAuthError(f"terminal {record.id} has no private CODEX_HOME")
        prepare_auth_scope(record.codex_home, account_source=self.account_source)
        return read_auth_state(record.codex_home)

    def _run_switch(
        self,
        wrapper: Path,
        selector: str,
        *,
        cwd: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(wrapper), "switch", selector],
            cwd=cwd,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=self.timeout,
        )

    def _rollback(
        self,
        record: SessionRecord,
        wrapper: Path,
        previous: CodexAccount | None,
    ) -> CodexAuthState:
        if previous is not None:
            try:
                self._run_switch(wrapper, previous.switch_selector, cwd=record.cwd)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            home = Path(record.codex_home or "").expanduser().resolve(strict=False)
            try:
                (home / "auth.json").unlink(missing_ok=True)
            except OSError:
                pass
            _clear_registry_active_account(home / "accounts" / "registry.json")
        return self.state(record)

    def switch(self, record: SessionRecord, account_query: str) -> CodexSwitchResult:
        state = self.state(record)
        try:
            account = resolve_account(state, account_query)
        except CodexAuthError as exc:
            return CodexSwitchResult(False, state, str(exc))
        previous = next((row for row in state.accounts if row.active), None)
        wrapper = Path(state.codex_home) / "bin" / "codex-auth"
        try:
            result = self._run_switch(wrapper, account.switch_selector, cwd=record.cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            rolled_back = self._rollback(record, wrapper, previous)
            detail = str(exc)
            if rolled_back.selected_account_key != state.selected_account_key:
                detail = f"{detail}; rollback could not restore the previous account"
            return CodexSwitchResult(False, rolled_back, detail)
        verified = self.state(record)
        if result.returncode == 0 and verified.selected_account_key == account.account_key:
            return CodexSwitchResult(True, verified)

        if result.returncode != 0:
            detail = (result.stderr or "codex-auth switch failed").strip().splitlines()[-1][:500]
        else:
            detail = "codex-auth returned success but the private registry did not verify the requested account"
        rolled_back = self._rollback(record, wrapper, previous)
        if rolled_back.selected_account_key != state.selected_account_key:
            detail = f"{detail}; rollback could not restore the previous account"
        return CodexSwitchResult(False, rolled_back, detail)
