"""Receipt-backed writable configuration for a confined native Codex tab.

The confined launcher and an explicit stopped-scope owning provisioning entry
initialize or refresh a view before any native process starts. Ordinary scope
preparation and live resume planning only inspect it.
The canonical source is never written. Native edits remain private; a changed
source and an edited view require explicit reconciliation instead of a merge.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .client_files import Directory, StagingError
from .codex_config_scope import CodexConfigScopeError, _scope_preparation_lock

RECEIPT = ".termdeck-native-config.json"
CONFIG = "config.toml"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
_PREFIX = b"# termdeck-private-native-config-v1 "
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
SOURCE_CONFLICT = (
    "System Codex configuration changed and this tab has private configuration edits; "
    "reconcile the private configuration before restarting or switching accounts"
)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stat(tree, name):
    try:
        return _identity(os.stat(name, dir_fd=tree.fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _read(tree, name, limit):
    with tree.file(name) as (fd, before):
        if stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > limit:
            raise CodexConfigScopeError("Private native configuration has unsafe permissions or size")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit or _identity(os.fstat(fd)) != _identity(before):
            raise CodexConfigScopeError("Private native configuration changed while reading")
    return bytes(raw), _identity(before)


def _toml(raw):
    try:
        tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise CodexConfigScopeError("Native configuration is not valid bounded TOML") from None


def _marker(receipt):
    return _PREFIX + receipt["identity"].encode("ascii") + b"\n"


def _binding(tree, source):
    home = tree.path
    if source == home or source in home.parents or home in source.parents:
        raise CodexConfigScopeError("Native configuration source and private scope must be separate")
    info = os.stat(".termdeck-canonical-home", dir_fd=tree.fd, follow_symlinks=False)
    if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or (home / ".termdeck-canonical-home").resolve(strict=True) != source):
        raise CodexConfigScopeError("Private native configuration source binding changed")


def _receipt(tree, source):
    try:
        raw, identity = _read(tree, RECEIPT, 8192)
    except FileNotFoundError:
        return None, None
    try:
        value = json.loads(raw)
        keys = {"version", "home", "home_identity", "source", "source_file", "source_sha256",
                "pristine_sha256", "previous_pristine_sha256", "identity", "state"}
        if (not isinstance(value, dict) or set(value) != keys or value["version"] != 1
                or value["home"] != str(tree.path) or value["source"] != str(source)
                or value["home_identity"] != [tree.identity.st_dev, tree.identity.st_ino]
                or value["state"] not in {"pending", "ready"}
                or not isinstance(value["identity"], str) or not _IDENTITY.fullmatch(value["identity"])
                or any(not isinstance(value[k], str) or not _DIGEST.fullmatch(value[k])
                       for k in ("source_sha256", "pristine_sha256"))
                or value["previous_pristine_sha256"] is not None and (
                    value["state"] != "pending" or not isinstance(value["previous_pristine_sha256"], str)
                    or not _DIGEST.fullmatch(value["previous_pristine_sha256"]))
                or value["source_file"] is not None and (
                    not isinstance(value["source_file"], str) or not Path(value["source_file"]).is_absolute()
                    or ".." in Path(value["source_file"]).parts)):
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise CodexConfigScopeError("Private native configuration receipt is invalid or belongs to another scope") from None
    return value, identity


def _current(tree, source, receipt):
    identity = _stat(tree, CONFIG)
    if identity is None or stat.S_ISLNK(identity[2]):
        if receipt is not None and (receipt["state"] != "pending" or receipt["previous_pristine_sha256"]):
            raise CodexConfigScopeError("Managed private native configuration was removed or replaced by a link")
        if identity is not None:
            path = tree.path / CONFIG
            if (identity[3] != os.geteuid() or identity[5] != 1
                    or path.resolve(strict=True) != (source / CONFIG).resolve(strict=True)):
                raise CodexConfigScopeError("Private native configuration link has a foreign target")
        return None, identity
    if receipt is None:
        raise CodexConfigScopeError("Private native configuration has no TermDeck provenance receipt")
    raw, identity = _read(tree, CONFIG, MAX_CONFIG_BYTES)
    if not raw.startswith(_marker(receipt)):
        raise CodexConfigScopeError("Private native configuration marker belongs to another view or was removed")
    _toml(raw)
    if receipt["state"] == "pending" and _sha(raw) not in {
            receipt["pristine_sha256"], receipt["previous_pristine_sha256"]}:
        raise CodexConfigScopeError("Private native configuration changed during an unfinished refresh; reconcile it before restart")
    return raw, identity


@contextmanager
def _scope(home, source):
    tree = None
    try:
        home, source = Path(home), Path(source)
        if not source.is_absolute() or source.resolve(strict=True) != source:
            raise CodexConfigScopeError("Native configuration source is not canonical")
        tree = Directory(home)
        _binding(tree, source)
        yield tree, source
        tree.check()
    except (OSError, StagingError, RuntimeError) as exc:
        if isinstance(exc, CodexConfigScopeError):
            raise
        raise CodexConfigScopeError("Private native configuration authority cannot be validated") from None
    finally:
        if tree is not None:
            tree.close()


def is_managed_private_config(home, source):
    """Read-only recognition used by ordinary configuration preparation.

    No receipt leaves legacy behavior intact. A receipt can never bless a
    foreign file, unsafe object, or another home/source. Source refresh is a
    separate pre-launch or stopped-scope operation and is never performed here.
    """
    if not os.path.lexists(Path(home) / RECEIPT):
        return False
    with _scope(home, source) as (tree, source):
        receipt, _ = _receipt(tree, source)
        if receipt is None:
            raise CodexConfigScopeError("Private native configuration receipt disappeared")
        raw, _ = _current(tree, source, receipt)
        return raw is not None


def _source_bytes(source, source_file):
    """Read an already grant-validated canonical target without following links."""
    declared = source / CONFIG
    if source_file is None:
        if os.path.lexists(declared):
            raise CodexConfigScopeError("System Codex configuration appeared during preparation")
        return b""
    target = Path(source_file)
    if not target.is_absolute() or declared.resolve(strict=True) != target:
        raise CodexConfigScopeError("System Codex configuration target changed")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
            Directory._check_ancestor(os.fstat(directory))
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > MAX_CONFIG_BYTES - 128):
            raise CodexConfigScopeError("System Codex configuration has unsafe authority or size")
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES - 128:
            chunk = os.read(fd, min(65536, MAX_CONFIG_BYTES - 127 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if (len(raw) > MAX_CONFIG_BYTES - 128 or _identity(before) != _identity(os.fstat(fd))
                or _identity(before) != _identity(os.stat(target.name, dir_fd=directory, follow_symlinks=False))
                or declared.resolve(strict=True) != target):
            raise CodexConfigScopeError("System Codex configuration changed while reading")
        _toml(bytes(raw))
        return bytes(raw)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _publish(tree, name, raw, expected):
    temporary = ".termdeck-native-config-" + uuid.uuid4().hex + ".tmp"
    try:
        with tree.file(temporary, create=True) as (fd, _):
            remaining = memoryview(raw)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError("short private configuration write")
                remaining = remaining[count:]
            os.fsync(fd)
        tree.check()
        if _stat(tree, name) != expected:
            raise CodexConfigScopeError("Private native configuration changed before publication")
        os.replace(temporary, name, src_dir_fd=tree.fd, dst_dir_fd=tree.fd)
        os.fsync(tree.fd)
        return _stat(tree, name)
    finally:
        try:
            os.unlink(temporary, dir_fd=tree.fd)
        except FileNotFoundError:
            pass


def _receipt_bytes(receipt):
    return (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()


def prepare_private_config(home, source, source_file, *, write=True, expected_source_sha256=None):
    """Initialize/refresh before launch, or preflight without writes.

    The caller validates ``source_file`` with the same protected-target grant
    policy used for shared mounts before this function may read it. Mutating
    calls run only before starting a new native process in this exact scope;
    live resume/account-switch planning passes ``write=False``. An explicit
    owning provisioning request can additionally bind the exact source digest;
    mismatch is refused before any receipt or configuration publication.
    """
    if expected_source_sha256 is not None and (
            not isinstance(expected_source_sha256, str) or not _DIGEST.fullmatch(expected_source_sha256)):
        raise CodexConfigScopeError("An exact expected configuration source digest is required")
    with _scope(home, source) as (tree, source):
        # Share the configuration preparer's lock. Read-only recognition does
        # not reacquire it, so it is also safe from within scope preparation.
        with _scope_preparation_lock(tree.path) if write else nullcontext():
            receipt, receipt_identity = _receipt(tree, source)
            current, current_identity = _current(tree, source, receipt)
            defaults = _source_bytes(source, source_file)
            source_digest = _sha(defaults)
            if expected_source_sha256 is not None and source_digest != expected_source_sha256:
                raise CodexConfigScopeError("System Codex configuration differs from the expected source digest")
            source_file = str(source_file) if source_file is not None else None
            unchanged = receipt is not None and receipt["source_sha256"] == source_digest and receipt["source_file"] == source_file
            if receipt is not None and receipt["state"] == "ready" and unchanged:
                return "ready"
            if current is not None and _sha(current) not in {
                    receipt["pristine_sha256"], receipt["previous_pristine_sha256"]}:
                raise CodexConfigScopeError(SOURCE_CONFLICT)
            if not write:
                return "refresh-required" if receipt else "initialization-required"
            updated = {"version": 1, "home": str(tree.path),
                       "home_identity": [tree.identity.st_dev, tree.identity.st_ino], "source": str(source),
                       "source_file": source_file, "source_sha256": source_digest,
                       "identity": receipt["identity"] if receipt else uuid.uuid4().hex,
                       "state": "pending", "previous_pristine_sha256": _sha(current) if current is not None else None}
            payload = _marker(updated) + defaults
            updated["pristine_sha256"] = _sha(payload)
            pending_identity = _publish(tree, RECEIPT, _receipt_bytes(updated), receipt_identity)
            _publish(tree, CONFIG, payload, current_identity)
            actual, _ = _read(tree, CONFIG, MAX_CONFIG_BYTES)
            if actual != payload:
                raise CodexConfigScopeError("Private native configuration changed during publication")
            updated.update(state="ready", previous_pristine_sha256=None)
            _publish(tree, RECEIPT, _receipt_bytes(updated), pending_identity)
            return "refreshed" if receipt else "initialized"
