"""Credential-content observations and guarded TermDeck catalogue refreshes.

No token bytes or decoded token claims leave this module. Local identity matching
does not authenticate a token with its provider. Reads never repair credentials.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class CredentialError(ValueError):
    pass


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise CredentialError("Credential JSON contains duplicate fields")
        result[key] = value
    return result


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > 1024 * 1024):
            raise CredentialError("Credential storage is unsafe")
        data = os.read(fd, 1024 * 1024 + 1)
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(data) > 1024 * 1024 or identity(before) != identity(after) or identity(after) != identity(path.lstat()):
            raise CredentialError("Credentials changed during observation")
        return data
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Credential:
    data: bytes
    digest: str
    identity: str | None
    refreshed: float | None


def read(path: Path) -> Credential:
    data = _read(path)
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError()
        tokens = value.get("tokens")
        stable = None
        if tokens is not None:
            if not isinstance(tokens, dict) or not any(isinstance(tokens.get(k), str) and tokens[k] for k in ("refresh_token", "access_token", "id_token")):
                raise ValueError()
            claims = {}
            for name in ("id_token", "access_token"):
                token = tokens.get(name)
                if not isinstance(token, str) or token.count(".") != 2:
                    continue
                encoded = token.split(".")[1]
                if len(encoded) > 65536:
                    raise ValueError()
                parsed = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)), object_pairs_hook=_unique)
                if not isinstance(parsed, dict):
                    raise ValueError()
                for key in ("sub", "iss"):
                    if key in parsed:
                        if not isinstance(parsed[key], str) or (key in claims and claims[key] != parsed[key]):
                            raise ValueError()
                        claims[key] = parsed[key]
                auth = parsed.get("https://api.openai.com/auth", {})
                if not isinstance(auth, dict):
                    raise ValueError()
                if "chatgpt_account_id" in auth:
                    account = auth["chatgpt_account_id"]
                    if not isinstance(account, str) or ("account" in claims and claims["account"] != account):
                        raise ValueError()
                    claims["account"] = account
            account = tokens.get("account_id")
            if account is not None:
                if not isinstance(account, str) or not account or ("account" in claims and claims["account"] != account):
                    raise ValueError()
                claims["account"] = account
            # A provider principal and account are both needed for refreshed
            # token matching. Account ID alone may be shared by multiple users.
            if claims.get("sub") and claims.get("account"):
                stable = hashlib.sha256(json.dumps(claims, sort_keys=True).encode()).hexdigest()
        elif not any(isinstance(value.get(k), str) and value[k] for k in ("OPENAI_API_KEY", "token")):
            raise ValueError()
        refreshed = value.get("last_refresh")
        if refreshed is not None:
            if not isinstance(refreshed, str):
                raise ValueError()
            parsed_time = datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                raise ValueError()
            refreshed = parsed_time.timestamp()
        # Canonical JSON digest tolerates harmless formatting changes.
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return Credential(data, digest, stable, refreshed)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise CredentialError("Credentials are malformed or internally conflicting") from exc


@dataclass(frozen=True)
class Observation:
    key: str | None
    status: str
    credential: Credential | None = None
    saved: Credential | None = None


def observe(home: Path, credentials: dict[str, Path]) -> Observation:
    auth = home / "auth.json"
    try:
        if auth.is_symlink():
            target = auth.resolve(strict=True)
            matches = [key for key, path in credentials.items() if path.resolve(strict=True) == target]
            if len(matches) == 1:
                credential = read(target)
                return Observation(matches[0], "current", credential, credential)
            # Older TermDeck scopes have private catalogue copies. Recognize
            # their content against the current catalogue without granting an
            # arbitrary symlink target or trusting the old selection marker.
            accounts = home / "accounts"
            info = accounts.lstat()
            if (matches or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or target.parent != accounts.resolve(strict=True)):
                return Observation(None, "conflict")
            current = read(target)
        else:
            current = read(auth)
    except FileNotFoundError:
        return Observation(None, "revoked" if auth.is_symlink() else "unselected")
    except (OSError, CredentialError, RuntimeError):
        return Observation(None, "malformed")
    candidates = []
    for key, path in credentials.items():
        try:
            saved = read(path)
        except (OSError, CredentialError):
            continue
        if saved.digest == current.digest or (current.identity is not None and saved.identity == current.identity):
            candidates.append((key, saved))
    if len(candidates) != 1:
        return Observation(None, "conflict" if candidates else "unmatched", current)
    key, saved = candidates[0]
    if current.digest == saved.digest:
        status = "current"
    elif current.refreshed is None or saved.refreshed is None or current.refreshed == saved.refreshed:
        status = "conflict"
    else:
        status = "refreshed" if current.refreshed > saved.refreshed else "stale"
    return Observation(key, status, current, saved)


def sync_quiescent(home: Path, source: Path, credentials: dict[str, Path]) -> str:
    """Publish a newer private snapshot only under explicit source policy.

    Caller must quiesce private writers and hold the scope transaction lock.
    The catalogue lock serializes TermDeck refreshes across tabs; the prepublish
    digest checks detect unsupported external writers and fail closed.
    """
    observation = observe(home, credentials)
    if observation.status in {"current", "unselected"}:
        return observation.status
    if observation.status != "refreshed":
        raise CredentialError(f"Private credential snapshot is {observation.status}; preserved for review")
    try:
        policy_path = source / ".termdeck-account-authority.json"
        if policy_path.lstat().st_mode & 0o077:
            raise CredentialError("TermDeck catalogue policy must be private")
        policy = json.loads(_read(policy_path), object_pairs_hook=_unique)
    except (OSError, ValueError) as exc:
        raise CredentialError("Newer private credentials preserved; TermDeck catalogue refresh policy is required") from exc
    if policy != {"version": 1, "owner": "termdeck", "refresh_writes": True}:
        raise CredentialError("TermDeck catalogue refresh is not authorized")
    lock = source / ".termdeck-account-refresh.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise CredentialError("Catalogue refresh lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        latest = observe(home, credentials)
        if latest.status == "current":
            return "current"
        if latest.status != "refreshed" or latest.key != observation.key or latest.credential.digest != observation.credential.digest:
            raise CredentialError("Concurrent credential refresh conflict; private snapshot preserved")
        target = credentials[latest.key]
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".termdeck-refresh-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(temporary_fd, "wb") as stream:
                stream.write(latest.credential.data); stream.flush(); os.fsync(stream.fileno())
            # Reapply the same authority checks used for the initial read. A
            # legacy auth.json may be a safe link into the private catalogue;
            # read() must continue to refuse links and read the validated
            # resolved regular file through observe().
            checked = observe(home, credentials)
            if (read(target).digest != latest.saved.digest
                    or checked.status != "refreshed" or checked.key != latest.key
                    or checked.credential is None
                    or checked.credential.digest != latest.credential.digest):
                raise CredentialError("Credentials changed before publication; private snapshot preserved")
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return "synchronized"
    finally:
        os.close(fd)


def process_start(pid: int) -> int | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return int(data[19]) if data[0] not in {"Z", "X"} else None
    except (OSError, ValueError, IndexError):
        return None


def _codex_processes(root_pid: int) -> tuple[tuple[int, int], ...] | None:
    """Observe Codex process incarnations without retaining command arguments."""
    from .resources import _process_argv, process_tree
    pids = process_tree(root_pid)
    if not pids:
        return None
    result = []
    for pid in pids:
        start = process_start(pid)
        argv = _process_argv(pid)
        if start is None or argv is None:
            return None
        if not argv:
            continue
        executable = Path(argv[0]).name.casefold()
        is_codex = executable == "codex" or executable.startswith("codex-")
        if executable in {"node", "nodejs"} and len(argv) > 1:
            is_codex = Path(argv[1]).name.casefold() == "codex"
        if is_codex:
            if process_start(pid) != start:
                return None
            result.append((pid, start))
    return tuple(sorted(result))


def activation_receipt(
    home: Path,
    scope: str,
    account_key: str | None,
    pid: int | None,
    *,
    kind: str,
    expected_digest: str | None,
) -> None:
    """Record the guarded lifecycle kind, never claim provider memory access."""
    from .codex_auth import _write_private_text
    if kind not in {"stopped", "idle-shell", "codex-restart"}:
        raise CredentialError("Account activation has no verified lifecycle kind")
    if (kind == "stopped") != (pid is None):
        raise CredentialError("Account activation process does not match its lifecycle")
    auth = home / "auth.json"
    credential = read(auth.resolve(strict=True)) if auth.exists() else None
    if (credential.digest if credential else None) != expected_digest:
        raise CredentialError("Credentials changed during account activation; inspect before retrying")
    start = process_start(pid) if pid else None
    if pid is not None and start is None:
        raise CredentialError("Account activation process is no longer observable")
    codex = _codex_processes(pid) if pid is not None else ()
    if codex is None or (pid is not None and process_start(pid) != start):
        raise CredentialError("Account activation process tree is no longer observable")
    if (kind == "codex-restart") != bool(codex):
        raise CredentialError("Account activation process tree changed after handoff")
    payload = {"version": 2, "kind": kind, "scope": scope, "pid": pid, "start": start,
               "account_key": account_key, "credential_digest": expected_digest,
               "codex_processes": codex}
    _write_private_text(home / ".termdeck-auth-runtime.json", json.dumps(payload, sort_keys=True) + "\n")


def runtime_observation(
    home: Path,
    scope: str,
    effective_digest: str | None,
    *,
    record_status: str | None = None,
    record_pid: int | None = None,
) -> dict:
    unknown = {"running_account_key": None, "running_account_state": "unobserved", "running_pid": None,
               "running_restart_required": True}
    try:
        receipt = json.loads(_read(home / ".termdeck-auth-runtime.json"), object_pairs_hook=_unique)
        if not isinstance(receipt, dict) or receipt.get("version") != 2 or receipt.get("scope") != scope:
            return unknown
        kind = receipt.get("kind")
        if kind not in {"stopped", "idle-shell", "codex-restart"}:
            return unknown
        pid, start = receipt.get("pid"), receipt.get("start")
        if kind == "stopped":
            # A stopped receipt has no process incarnation to check. Its
            # evidence expires as soon as the owning record starts/reopens.
            if pid is not None or start is not None or record_pid is not None or record_status not in {"stopped", "archived"}:
                return unknown
            return {**unknown, "running_account_state": "stopped", "running_restart_required": False}
        if record_status in {"stopped", "archived"} or (record_pid is not None and record_pid != pid):
            return {**unknown, "running_account_state": "stale-process"}
        if type(pid) is not int or type(start) is not int or process_start(pid) != start:
            return {**unknown, "running_account_state": "stale-process"}
        # A receipt binds a process incarnation. Recheck its inherited scope;
        # PID existence alone is never evidence that it runs with this home.
        environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if ("CODEX_HOME=" + str(home)).encode() not in environment or process_start(pid) != start:
            return unknown
        codex = _codex_processes(pid)
        if process_start(pid) != start or codex is None:
            return unknown
        if not codex:
            return {**unknown, "running_account_state": "no-codex", "running_pid": pid,
                    "running_restart_required": False}
        if kind != "codex-restart":
            # A Codex child launched after an idle-shell handoff has no guarded
            # restart receipt of its own; the shell's readiness is not proof.
            return {**unknown, "running_pid": pid}
        if receipt.get("codex_processes") != [list(process) for process in codex]:
            # A shell or Node launcher can outlive its Codex child. A later
            # child must not inherit the previous guarded restart's evidence.
            return {**unknown, "running_pid": pid}
        changed = receipt.get("credential_digest") != effective_digest
        return {"running_account_key": receipt.get("account_key"),
                "running_account_state": "credentials-changed" if changed else "restart-observed",
                "running_pid": pid, "running_restart_required": changed}
    except (OSError, ValueError, TypeError):
        return unknown
