"""Prepare and publish an explicitly reviewed native Codex policy upgrade.

This operator command writes only web-launch-policy.json. Preparation never
changes its target. Publication is serialized by a private native flock and
requires the exact reviewed preimage; unrelated policy fields are preserved.
It does not install Codex, change accounts, start terminals, or restart services.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import uuid

from .agent_launch import POLICY, policy
from .client_files import Directory, StagingError, _check_file


SCHEMA = "t1000.codex-policy-upgrade.v1"
LOCK = ".web-launch-policy.lock"
AUTO_PAUSE = ".web-launch-policy-auto-pause.json"
AUTO_PAUSE_SCHEMA = "t1000.codex-policy-auto-refresh-pause.v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
NPM_PREFLIGHT = Path("/home/arch/.local/libexec/codex-npm-platform-preflight.py")
NPM_PREFLIGHT_SHA256 = "f50a5b962bd651fe215ebbf96792e74cfbe7c14f497bb1952d8f1dea583890db"
_NPM_TRIPLE = "x86_64-unknown-linux-musl"
_NPM_MAIN = Path("lib/node_modules/@openai/codex")
_NPM_LAYOUTS = {
    "nested": Path("lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64")
              / "vendor" / _NPM_TRIPLE / "bin/codex",
    "flat": Path("lib/node_modules/@openai/codex-linux-x64") / "vendor" / _NPM_TRIPLE / "bin/codex",
    "bundled": _NPM_MAIN / "vendor" / _NPM_TRIPLE / "bin/codex",
}


class PolicyUpgradeError(ValueError):
    """A reviewed policy precondition could not be established."""


def _absolute(value):
    text = os.fspath(value)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or str(path) != text:
        raise PolicyUpgradeError("Use an exact absolute path without aliases or traversal")
    return path


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyUpgradeError("Duplicate JSON fields are not accepted")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PolicyUpgradeError("Invalid policy or qualification JSON") from exc


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _read(tree, name, maximum=65536):
    with tree.file(name) as (fd, before):
        data = os.read(fd, maximum + 1)
        if len(data) > maximum or _identity(os.fstat(fd)) != _identity(before):
            raise PolicyUpgradeError("Policy evidence is oversized or changed while reading")
    tree.check()
    return data, _identity(before)


def _write_new(tree, name, data):
    with tree.file(name, create=True, write=True) as (fd, _):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Policy evidence write made no progress")
            view = view[written:]
        os.fsync(fd)
    os.fsync(tree.fd)


class _NativeDirectory(Directory):
    """Pinned, non-writable npm ancestors; package directories need not be 0700."""
    def _check_dir(self, info):
        self._check_ancestor(info)

    def check(self):
        # Directory.check intentionally requires a private staging root. npm
        # directories have the distinct owned, non-writable package contract.
        other = _NativeDirectory(self.path)
        try:
            if other.chain != self.chain:
                raise PolicyUpgradeError("Native package directory ancestry changed")
        finally:
            other.close()


@contextmanager
def _native_file(path):
    tree = _NativeDirectory(path.parent)
    descriptor = None
    try:
        # ubs:ignore[py.io.open-missing-with] -- this descriptor is revalidated, then explicitly closed in finally.
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                             | os.O_NONBLOCK, dir_fd=tree.fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise PolicyUpgradeError("Native package files must be owned, regular and non-writable by others")
        yield tree, descriptor, info
        tree.check()
        if (_identity(os.fstat(descriptor)) != _identity(info)
                or _identity(os.stat(path.name, dir_fd=tree.fd, follow_symlinks=False)) != _identity(info)):
            raise PolicyUpgradeError("Native package identity changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        tree.close()


def _fd_digest(fd, maximum):
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(fd, 1024 * 1024):  # ubs:ignore[py.perf.io-in-loop] -- A bounded one-MiB stream is required to hash large package files without loading them into memory.
        size += len(chunk)
        if size > maximum:
            raise PolicyUpgradeError("Native package file exceeds the qualification limit")
        digest.update(chunk)
    return digest.hexdigest()


def _probe(fd, probe_root):
    # Execute the already-opened ELF, not a second resolution of a mutable path.
    # No inherited credentials, launcher overrides, loader variables or config.
    with tempfile.TemporaryDirectory(prefix="native-probe-", dir=probe_root) as temporary:
        root = Path(temporary)
        names = {"HOME": "home", "CODEX_HOME": "codex", "XDG_CONFIG_HOME": "config",
                 "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state",
                 "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "runtime", "TMPDIR": "tmp"}
        env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        for key, name in names.items():
            path = root / name
            path.mkdir(mode=0o700)
            env[key] = str(path)
        results = []
        for option in ("--version", "--help"):
            with tempfile.TemporaryFile(dir=root) as output:
                descriptor_path = f"/proc/self/fd/{fd}"
                result = subprocess.run(  # ubs:ignore[python.taint.command] -- This intentionally probes the already-open npm ELF: _native_file pins its no-follow inode, owner, mode and path, qualify_native checks official package metadata/version, and the same descriptor digest is rechecked after the probe. argv[0] is only the descriptor path; shell is disabled and the environment is isolated.
                    [descriptor_path, option], executable=descriptor_path,
                    pass_fds=(fd,), cwd=root, env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=output, check=False, shell=False, timeout=15)
                output.seek(0)
                raw = output.read(131073)
            if result.returncode != 0 or len(raw) > 131072:
                raise PolicyUpgradeError("Native version/help probe failed or exceeded its output bound")
            results.append(raw.decode("utf-8", errors="strict"))
        return results


def _prefix_for_native(native):
    native = _absolute(native)
    matches = []
    for layout, suffix in _NPM_LAYOUTS.items():
        if native.parts[-len(suffix.parts):] == suffix.parts:
            prefix = Path(*native.parts[:-len(suffix.parts)])
            matches.append((layout, prefix))
    if len(matches) != 1:
        raise PolicyUpgradeError("Native executable is outside the supported same-prefix npm layouts")
    return matches[0][1]


def is_npm_native_path(native):
    """Tell current admission whether the stored path claims a supported npm layout."""
    try:
        parts = Path(os.fspath(native)).parts
    except (TypeError, ValueError, OSError):
        return False
    return any(parts[-len(suffix.parts):] == suffix.parts for suffix in _NPM_LAYOUTS.values())


def _resolution(value, entrypoint, expected_prefix):
    fields = {"prefix", "package", "version", "layout", "platform_package", "native"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PolicyUpgradeError("Official npm preflight returned an invalid result")
    prefix = _absolute(value["prefix"])
    entrypoint = _absolute(entrypoint)
    if prefix != expected_prefix or entrypoint != prefix / "bin/codex":
        raise PolicyUpgradeError("Official npm runtime escaped the approved prefix")
    package = prefix / _NPM_MAIN
    if value["package"] != str(package):
        raise PolicyUpgradeError("Official npm main package is outside the approved prefix")
    version = value["version"]
    layout = value["layout"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version) or layout not in _NPM_LAYOUTS:
        raise PolicyUpgradeError("Official npm runtime version or layout is unsupported")
    native = _absolute(value["native"])
    if native != prefix / _NPM_LAYOUTS[layout]:
        raise PolicyUpgradeError("Official npm runtime path does not match its supported layout")
    platform_package = value["platform_package"]
    if layout == "bundled":
        expected_platform = None
    else:
        expected_platform = str(native.parents[3] / "package.json")
    if platform_package != expected_platform:
        raise PolicyUpgradeError("Official npm platform package escaped the selected layout")
    return {"entrypoint": str(entrypoint), "prefix": str(prefix), "package": str(package),
            "version": version, "layout": layout, "platform_package": platform_package,
            "native": str(native)}


def _run_npm_preflight(entrypoint, expected_prefix):
    entrypoint = _absolute(entrypoint)
    expected_prefix = _absolute(expected_prefix)
    with _native_file(NPM_PREFLIGHT) as (_, helper_fd, helper_info):
        helper_digest = _fd_digest(helper_fd, 2 * 1024 * 1024)
        if helper_digest != NPM_PREFLIGHT_SHA256:
            raise PolicyUpgradeError("Official npm preflight identity changed")
        os.lseek(helper_fd, 0, os.SEEK_SET)
        environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8"}
        try:
            result = subprocess.run(  # ubs:ignore[python.taint.command] -- Fixed Python argv, hash-pinned helper fd, shell=False, sanitized environment; entrypoint is constrained to the previously approved npm prefix.
                [sys.executable, "-I", "-S", "-B", f"/proc/self/fd/{helper_fd}",
                 "--json", str(entrypoint)], pass_fds=(helper_fd,), stdin=subprocess.DEVNULL,
                capture_output=True, check=False, shell=False, timeout=8, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyUpgradeError("Official npm preflight could not resolve the current runtime") from exc
        if result.returncode != 0 or len(result.stdout) > 32768:
            raise PolicyUpgradeError("Official npm preflight rejected the current runtime")
        value = _json(result.stdout)
        if _identity(os.fstat(helper_fd)) != _identity(helper_info):
            raise PolicyUpgradeError("Official npm preflight identity changed")
    return _resolution(value, entrypoint, expected_prefix)


def current_npm_runtime(previous_native):
    """Resolve today's official Codex entrypoint under the prior approved prefix."""
    prefix = _prefix_for_native(previous_native)
    entrypoint = prefix / "bin/codex"
    resolved = _run_npm_preflight(entrypoint, prefix)
    return resolved


def native_digest(native):
    native = _absolute(native)
    with _native_file(native) as (_, fd, info):
        if info.st_size > 512 * 1024 * 1024:
            raise PolicyUpgradeError("Native runtime exceeds the qualification size limit")
        header = os.pread(fd, 20, 0)
        if (not info.st_mode & 0o111 or header[:6] != b"\x7fELF\x02\x01"
                or header[18:20] != b"\x3e\x00"):
            raise PolicyUpgradeError("Native runtime must be an executable Linux x86_64 ELF")
        return _fd_digest(fd, 512 * 1024 * 1024)


def qualify_native(native, version, probe_root, *, entrypoint=None, expected_prefix=None):
    native = _absolute(native)
    inferred_prefix = _prefix_for_native(native)
    prefix = _absolute(expected_prefix) if expected_prefix is not None else inferred_prefix
    if prefix != inferred_prefix or (platform.system() != "Linux" or platform.machine() != "x86_64"):
        raise PolicyUpgradeError("This producer requires the approved npm prefix on Linux x86_64")
    entrypoint = _absolute(entrypoint) if entrypoint is not None else prefix / "bin/codex"
    resolution = _run_npm_preflight(entrypoint, prefix)
    if native != Path(resolution["native"]):
        raise PolicyUpgradeError("Native executable is not the current official entrypoint resolution")
    if version is not None and (not isinstance(version, str) or version != resolution["version"]):
        raise PolicyUpgradeError("Requested version differs from the current official npm package")
    version = resolution["version"]
    package_paths = [prefix / _NPM_MAIN / "package.json"]
    if resolution["platform_package"] is not None:
        package_paths.append(Path(resolution["platform_package"]))
    package_paths.append(native)
    evidence = {}
    with ExitStack() as stack:
        opened = [stack.enter_context(_native_file(path)) for path in package_paths]
        owners = {info.st_uid for _, _, info in opened}
        if len(owners) != 1:
            raise PolicyUpgradeError("Native executable and package owners disagree")
        metadata = []
        for path, (tree, fd, info) in zip(package_paths, opened):
            limit = 512 * 1024 * 1024 if path == native else 1024 * 1024
            digest = _fd_digest(fd, limit)
            evidence[str(path)] = {"identity": _identity(info), "sha256": digest,
                                   "ancestry": [list(row) for row in tree.chain]}
            os.lseek(fd, 0, os.SEEK_SET)
            if path != native:
                metadata.append(_json(os.read(fd, limit + 1)))
        parent = metadata[0]
        if (not isinstance(parent, dict) or parent.get("name") != "@openai/codex"
                or parent.get("version") != version
                or not isinstance(parent.get("bin"), dict)
                or parent["bin"].get("codex") != "bin/codex.js"):
            raise PolicyUpgradeError("Native npm main package metadata changed after official resolution")
        _, native_fd, info = opened[-1]
        os.lseek(native_fd, 0, os.SEEK_SET)
        header = os.read(native_fd, 20)
        if (not info.st_mode & 0o111 or header[:6] != b"\x7fELF\x02\x01"
                or header[18:20] != b"\x3e\x00"):
            raise PolicyUpgradeError("Native runtime must be an executable Linux x86_64 ELF")
        actual_version, help_text = _probe(native_fd, probe_root)
        if actual_version.strip() != "codex-cli " + version:
            raise PolicyUpgradeError("Live native version does not match the official package")
        required = ["--no-daemon", "--no-alt-screen", "--config"]
        if not all(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) for flag in required):
            raise PolicyUpgradeError("Native help does not advertise required private-launch arguments")
        for path, (_, fd, _) in zip(package_paths, opened):
            limit = 512 * 1024 * 1024 if path == native else 1024 * 1024
            if _fd_digest(fd, limit) != evidence[str(path)]["sha256"]:
                raise PolicyUpgradeError("Native source changed during live qualification")
    return {"executable": str(native), "entrypoint": resolution["entrypoint"],
            "prefix": resolution["prefix"], "package": resolution["package"],
            "version": version, "layout": resolution["layout"],
            "platform_package": resolution["platform_package"],
            "preflight_sha256": NPM_PREFLIGHT_SHA256,
            "sha256": evidence[str(native)]["sha256"], "files": evidence,
            "observed_version": actual_version.strip(), "required_arguments": required}


def _shape(value):
    if (not isinstance(value, dict) or set(value) != {"version", "workspaces", "codex"}
            or isinstance(value["version"], bool) or not isinstance(value["version"], int)
            or value["version"] != 1
            or not isinstance(value["workspaces"], list) or len(value["workspaces"]) > 64
            or not isinstance(value["codex"], dict)
            or set(value["codex"]) != {"executable", "sha256", "account_source"}
            or not isinstance(value["codex"]["sha256"], str)
            or not _HEX.fullmatch(value["codex"]["sha256"])):
        raise PolicyUpgradeError("An existing explicit native Codex policy is required")
    _absolute(value["codex"]["executable"])
    _absolute(value["codex"]["account_source"])
    return value


def prepare(state_dir, expected_policy_sha256, native, version, output, *, automation="operator-reviewed",
            entrypoint=None, expected_prefix=None):
    state_dir, output = _absolute(state_dir), _absolute(output)
    if (not _HEX.fullmatch(expected_policy_sha256)
            or automation not in {"operator-reviewed", "launch-time"}):
        raise PolicyUpgradeError("An exact old-policy SHA-256 is required")
    with ExitStack() as stack:
        state = stack.enter_context(_tree(state_dir))
        old, identity = _read(state, POLICY)
        if _digest(old) != expected_policy_sha256:
            raise PolicyUpgradeError("Old policy digest changed")
        value = _shape(_json(old))
        parent = stack.enter_context(_tree(output.parent))
        os.mkdir(output.name, mode=0o700, dir_fd=parent.fd)
        os.fsync(parent.fd)
        bundle = stack.enter_context(_tree(output))
        _write_new(bundle, "before.json", old)
        native_proof = qualify_native(native, version, output, entrypoint=entrypoint,
                                      expected_prefix=expected_prefix)
        value["codex"]["executable"] = native_proof["executable"]
        value["codex"]["sha256"] = native_proof["sha256"]
        candidate = _encoded(value)
        _write_new(bundle, "after.json", candidate)
        with bundle.directory("validation", create=True):
            pass
        validation = stack.enter_context(_tree(output / "validation"))
        _write_new(validation, POLICY, candidate)
        policy(validation.path)  # Same unchanged admission as an actual launch.
        current, current_identity = _read(state, POLICY)
        if current != old or current_identity != identity:
            raise PolicyUpgradeError("Old policy changed during preparation")
        manifest = {"schema": SCHEMA, "state_dir": str(state_dir),
                    "automation": automation,
                    "state_ancestry": [list(row) for row in state.chain],
                    "before_sha256": _digest(old), "before_identity": identity,
                    "after_sha256": _digest(candidate), "native": native_proof}
        raw = _encoded(manifest)
        _write_new(bundle, "manifest.json", raw)
        parent.check()
    return {"ok": True, "action": "prepare", "bundle": str(output),
            "manifest_sha256": _digest(raw), "policy_changed": False,
            "before_sha256": _digest(old), "after_sha256": _digest(candidate),
            "native_version": native_proof["observed_version"]}


@contextmanager
def _tree(path):
    tree = Directory(path)
    try:
        yield tree
    finally:
        tree.close()


@contextmanager
def _locked(state):
    # ubs:ignore[py.io.open-missing-with] -- this raw descriptor owns the flock and is always closed in finally.
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                 | os.O_NONBLOCK, 0o600, dir_fd=state.fd)
    try:
        before = os.fstat(fd)
        _check_file(before, state.identity.st_dev)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PolicyUpgradeError("Another native policy publication holds the lock") from exc
        if _identity(os.stat(LOCK, dir_fd=state.fd, follow_symlinks=False)) != _identity(before):
            raise PolicyUpgradeError("Native policy lock identity changed")
        state.check()
        os.fsync(state.fd)
        yield
    finally:
        os.close(fd)


def _publish(state, expected, expected_identity, replacement):
    temporary = ".web-launch-policy-" + uuid.uuid4().hex
    _write_new(state, temporary, replacement)
    temporary_identity = _identity(os.stat(temporary, dir_fd=state.fd, follow_symlinks=False))
    try:
        current, identity = _read(state, POLICY)
        if current != expected or identity != expected_identity:
            raise PolicyUpgradeError("Policy compare-and-swap preimage changed")
        state.check()
        os.replace(temporary, POLICY, src_dir_fd=state.fd, dst_dir_fd=state.fd)
        os.fsync(state.fd)
        after, _ = _read(state, POLICY)
        if after != replacement:
            raise PolicyUpgradeError("Published policy changed; outcome requires inspection")
    finally:
        try:
            if _identity(os.stat(temporary, dir_fd=state.fd, follow_symlinks=False)) == temporary_identity:
                os.unlink(temporary, dir_fd=state.fd)
                os.fsync(state.fd)
        except FileNotFoundError:
            pass


def _read_optional(tree, name, maximum=65536):
    try:
        return _read(tree, name, maximum)
    except FileNotFoundError:
        return None, None


def _write_record(tree, name, replacement):
    temporary = ".web-launch-policy-record-" + uuid.uuid4().hex
    before, before_identity = _read_optional(tree, name)
    _write_new(tree, temporary, replacement)
    temporary_identity = _identity(os.stat(temporary, dir_fd=tree.fd, follow_symlinks=False))
    try:
        current, current_identity = _read_optional(tree, name)
        if current != before or current_identity != before_identity:
            raise PolicyUpgradeError("Automatic refresh pause record changed")
        tree.check()
        os.replace(temporary, name, src_dir_fd=tree.fd, dst_dir_fd=tree.fd)
        os.fsync(tree.fd)
        if _read(tree, name)[0] != replacement:
            raise PolicyUpgradeError("Automatic refresh pause record changed during publication")
    finally:
        try:
            if _identity(os.stat(temporary, dir_fd=tree.fd, follow_symlinks=False)) == temporary_identity:
                os.unlink(temporary, dir_fd=tree.fd)
                os.fsync(tree.fd)
        except FileNotFoundError:
            pass


def _pause_matches(tree, current, proof):
    raw, _ = _read_optional(tree, AUTO_PAUSE)
    if raw is None:
        return False
    pause = _json(raw)
    expected = {"schema", "policy_sha256", "executable", "native_sha256", "version",
                "bundle_manifest_sha256"}
    if (not isinstance(pause, dict) or set(pause) != expected
            or pause.get("schema") != AUTO_PAUSE_SCHEMA
            or not _HEX.fullmatch(str(pause.get("policy_sha256", "")))
            or not _HEX.fullmatch(str(pause.get("native_sha256", "")))
            or not _HEX.fullmatch(str(pause.get("bundle_manifest_sha256", "")))
            or not _VERSION.fullmatch(str(pause.get("version", "")))):
        raise PolicyUpgradeError("Automatic refresh pause record is malformed")
    return (pause["policy_sha256"] == _digest(current)
            and pause["executable"] == proof["executable"]
            and pause["native_sha256"] == proof["sha256"]
            and pause["version"] == proof["version"])


def publish(state_dir, bundle_path, manifest_sha256, *, rollback=False, automatic=False):
    state_dir, bundle_path = _absolute(state_dir), _absolute(bundle_path)
    if not _HEX.fullmatch(manifest_sha256):
        raise PolicyUpgradeError("An explicitly reviewed manifest SHA-256 is required")
    with ExitStack() as stack:
        bundle = stack.enter_context(_tree(bundle_path))
        raw, _ = _read(bundle, "manifest.json", 1024 * 1024)
        if _digest(raw) != manifest_sha256:
            raise PolicyUpgradeError("Reviewed manifest digest changed")
        manifest = _json(raw)
        if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
                or manifest.get("state_dir") != str(state_dir)
                or manifest.get("automation") not in {"operator-reviewed", "launch-time"}
                or automatic and manifest.get("automation") != "launch-time"
                or not automatic and not rollback and manifest.get("automation") == "launch-time"):
            raise PolicyUpgradeError("Policy manifest is not bound to this state directory")
        before, _ = _read(bundle, "before.json")  # ubs:ignore[py.perf.io-in-loop] -- One bounded backup read; the legacy substring rule matches 'for' inside before.json, not a loop.
        after, _ = _read(bundle, "after.json")
        if (_digest(before) != manifest.get("before_sha256")
                or _digest(after) != manifest.get("after_sha256")):
            raise PolicyUpgradeError("Policy backup or candidate digest changed")
        original, candidate = _shape(_json(before)), _shape(_json(after))
        proof = manifest["native"]
        permitted = copy.deepcopy(original)
        permitted["codex"].update(executable=proof["executable"], sha256=proof["sha256"])
        if candidate != permitted:
            raise PolicyUpgradeError("Policy candidate changes unrelated settings")
        state = stack.enter_context(_tree(state_dir))
        if [list(row) for row in state.chain] != manifest.get("state_ancestry"):
            raise PolicyUpgradeError("Policy state directory identity changed")
        with _locked(state):
            current, identity = _read(state, POLICY)
            expected, replacement = (after, before) if rollback else (before, after)
            if current != expected or (not rollback and identity != manifest["before_identity"]):
                raise PolicyUpgradeError("Reviewed policy preimage changed")
            if not rollback:
                actual = qualify_native(proof["executable"], proof["version"], bundle_path,
                                        entrypoint=proof["entrypoint"],
                                        expected_prefix=proof["prefix"])
                if actual != proof:
                    raise PolicyUpgradeError("Qualified native source changed before publication")
                with _tree(bundle_path / "validation") as validation:
                    if _read(validation, POLICY)[0] != after:
                        raise PolicyUpgradeError("Validation policy changed")
                policy(bundle_path / "validation")
                if automatic and _pause_matches(state, current, proof):
                    raise PolicyUpgradeError("Automatic refresh is paused after a matching rollback")
            bundle.check()
            if _read(bundle, "manifest.json", 1024 * 1024)[0] != raw:
                raise PolicyUpgradeError("Manifest changed during publication")
            if rollback:
                pause = {"schema": AUTO_PAUSE_SCHEMA, "policy_sha256": _digest(before),
                         "executable": proof["executable"], "native_sha256": proof["sha256"],
                         "version": proof["version"], "bundle_manifest_sha256": manifest_sha256}
                _write_record(state, AUTO_PAUSE, _encoded(pause))
            _publish(state, expected, identity, replacement)
    return {"ok": True, "action": "rollback" if rollback else "apply",
            "state_dir": str(state_dir), "policy_sha256": _digest(replacement),
            "services_restarted": False, "terminals_started": False}


def refresh_native_policy(state_dir, current_policy, native, entrypoint):
    """Qualify and CAS-refresh only the native digest during policy admission."""
    state_dir, native, entrypoint = _absolute(state_dir), _absolute(native), _absolute(entrypoint)
    if not isinstance(current_policy, bytes) or len(current_policy) > 65536:
        raise PolicyUpgradeError("Current policy bytes are invalid")
    output = state_dir / (".web-launch-policy-auto-" + uuid.uuid4().hex)
    prefix = _prefix_for_native(native)
    receipt = prepare(state_dir, _digest(current_policy), native, None, output,
                      automation="launch-time", entrypoint=entrypoint, expected_prefix=prefix)
    try:
        return publish(state_dir, output, receipt["manifest_sha256"], automatic=True)
    except PolicyUpgradeError:
        # A concurrent launch may have published the exact same candidate.
        # Accept only byte-for-byte convergence; a changed policy remains a conflict.
        with _tree(state_dir) as state:
            current, _ = _read(state, POLICY)
        with _tree(output) as bundle:
            candidate, _ = _read(bundle, "after.json")
        if current == candidate:
            return {"ok": True, "action": "launch-time-refresh-already-current",
                    "state_dir": str(state_dir), "policy_sha256": _digest(candidate),
                    "services_restarted": False, "terminals_started": False}
        raise


def add_arguments(parser):
    commands = parser.add_subparsers(dest="policy_action", required=True)
    preparation = commands.add_parser("prepare", help="qualify native identity and save a private review bundle")
    preparation.add_argument("--state-dir", required=True)
    preparation.add_argument("--expected-policy-sha256", required=True)
    preparation.add_argument("--native", required=True)
    preparation.add_argument("--version", help="Expected stable version; defaults to the main npm package metadata")
    preparation.add_argument("--output", required=True)
    preparation.set_defaults(func=command)
    for name in ("apply", "rollback"):
        child = commands.add_parser(name, help="publish only the exact reviewed policy transition")
        child.add_argument("--state-dir", required=True)
        child.add_argument("--bundle", required=True)
        child.add_argument("--manifest-sha256", required=True)
        child.set_defaults(func=command)


def command(args):
    try:
        if args.policy_action == "prepare":
            result = prepare(args.state_dir, args.expected_policy_sha256, args.native, args.version, args.output)
        else:
            result = publish(args.state_dir, args.bundle, args.manifest_sha256,
                             rollback=args.policy_action == "rollback")
    except (OSError, ValueError, TypeError, KeyError, StagingError, subprocess.TimeoutExpired) as exc:
        # Never expose probe output, credential content or arbitrary file bytes.
        print(json.dumps({"ok": False, "action": args.policy_action, "error_type": type(exc).__name__,
                          "error": str(exc) if isinstance(exc, (PolicyUpgradeError, StagingError))
                          else "Policy qualification failed; target and retained evidence require inspection"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
