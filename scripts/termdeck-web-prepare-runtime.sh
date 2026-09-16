#!/usr/bin/bash
set -euo pipefail
umask 077

restore_generation=
restore_unreceipted=0
verify_existing=0
if [ "$#" -gt 0 ]; then
    if [ "$#" -eq 1 ] && [ "$1" = "--verify-existing" ]; then
        verify_existing=1
    elif [ "$#" -eq 1 ] && [ "$1" = "--restore-unreceipted-tls-generation" ]; then
        restore_unreceipted=1
    elif [ "$#" -eq 2 ] && [ "$1" = "--restore-tls-generation" ] && [[ "$2" =~ ^[0-9a-f]{32}$ ]]; then
        restore_generation=$2
    else
        echo "usage: termdeck-web-prepare [--verify-existing|--restore-tls-generation <32 lowercase hex>|--restore-unreceipted-tls-generation]" >&2
        exit 2
    fi
fi

# The managed wrapper invokes the copy published at
# ``<runtime-root>/termdeck-lan/bin/termdeck-web-prepare``.  Derive the LAN
# root from that exact executable location so custom wrapper layouts never
# silently write the canonical /warp runtime.  An explicit environment value
# is accepted only for an intentionally direct invocation and must agree with
# the installed path when both are available.
runtime=
prepare_script=${BASH_SOURCE[0]}
if [[ "$prepare_script" == */bin/termdeck-web-prepare ]]; then
    derived_runtime=${prepare_script%/bin/termdeck-web-prepare}
    if [[ "$derived_runtime" != /* || "$derived_runtime" == *"//"* || "$derived_runtime" == */./* || "$derived_runtime" == */../* || "$derived_runtime" == "." || "$derived_runtime" == ".." ]]; then
        echo "TermDeck runtime preparation refused non-canonical installed root" >&2
        exit 1
    fi
    if [[ -n ${TERMDECK_RUNTIME_ROOT:-} && "$TERMDECK_RUNTIME_ROOT" != "$derived_runtime" ]]; then
        echo "TermDeck runtime preparation refused mismatched requested root" >&2
        exit 1
    fi
    runtime=$derived_runtime
elif [[ -n ${TERMDECK_RUNTIME_ROOT:-} ]]; then
    runtime=$TERMDECK_RUNTIME_ROOT
else
    echo "TermDeck runtime preparation requires an installed runtime root or TERMDECK_RUNTIME_ROOT" >&2
    exit 1
fi
if [[ "$runtime" != /* || "$runtime" == *"//"* || "$runtime" == */./* || "$runtime" == */../* || "$runtime" == "." || "$runtime" == ".." ]]; then
    echo "TermDeck runtime preparation refused non-canonical requested root" >&2
    exit 1
fi
tls="$runtime/tls"
nginx_root="$runtime/nginx"
bin="$runtime/bin"

# A max-facing broker uses this read-only operation after service start.  The
# runtime tree is deliberately 0700 and owned by termdeck-preparer, so max
# must consume the verified result through the same fixed principal boundary
# instead of reopening producer-owned paths directly.
if [ "$verify_existing" -eq 1 ]; then
    exec /usr/bin/python3 -I /usr/libexec/termdeck-preparer-state \
        --receipt "$runtime/build-receipt.json" --verify-existing
fi
# Create and normalize only the runtime directories needed below through
# no-follow directory descriptors.  The generated publication helper performs
# a fresh anchored walk later, but this preflight prevents setup itself from
# following a hostile runtime/nginx parent before that helper starts.  It also
# returns the descriptor-anchored directory identities to the publication
# helper.  A later real-directory replacement therefore fails closed instead
# of becoming that helper's new baseline.
runtime_guards=$(
/usr/bin/python3 -I - "$runtime" "$tls" "$nginx_root" "$nginx_root/logs" "$nginx_root/temp/client" "$nginx_root/temp/proxy" "$bin" <<'PY'
import json
import os
import stat
import sys

directory = getattr(os, "O_DIRECTORY", 0)
nofollow = getattr(os, "O_NOFOLLOW", 0)
if not directory or not nofollow:
    raise SystemExit("TermDeck runtime directory preparation requires O_DIRECTORY and O_NOFOLLOW")
flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def components(path: str) -> tuple[str, ...]:
    if not path or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise RuntimeError(f"not an absolute canonical path: {path!r}")
    result = tuple(path.split("/")[1:])
    if not result or any(not item or item in {".", ".."} or "/" in item or "\\" in item for item in result):
        raise RuntimeError(f"unsafe path: {path!r}")
    return result


def directory_identity(info: os.stat_result, *, path: str) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise RuntimeError(f"not a real directory: {path}")
    return int(info.st_dev), int(info.st_ino), stat.S_IMODE(info.st_mode), int(info.st_uid)


opened: dict[tuple[str, ...], tuple[int, tuple[int, int, int, int]]] = {}


def assert_bound() -> None:
    for parts, (descriptor, expected) in opened.items():
        path = "/" + "/".join(parts)
        observed = directory_identity(os.fstat(descriptor), path=path)
        if parts:
            parent = opened[parts[:-1]][0]
            linked = directory_identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False), path=path)
        else:
            linked = observed
        if observed != expected or linked != expected:
            raise RuntimeError(f"directory identity changed during preflight: {path}")


def open_directory(parts: tuple[str, ...], *, create: bool) -> int:
    assert_bound()
    for index, item in enumerate(parts):
        prefix = parts[:index + 1]
        if prefix in opened:
            continue
        parent = opened[prefix[:-1]][0]
        try:
            descriptor = os.open(item, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise RuntimeError(f"required directory is missing: /{'/'.join(parts)}")
            assert_bound()
            os.mkdir(item, 0o700, dir_fd=parent)
            os.fsync(parent)
            assert_bound()
            descriptor = os.open(item, flags, dir_fd=parent)
        try:
            expected = directory_identity(os.fstat(descriptor), path="/" + "/".join(prefix))
        except BaseException:
            os.close(descriptor)
            raise
        opened[prefix] = descriptor, expected
        assert_bound()
    return opened[parts][0]


def prepare(path: str, *, exact_mode: bool, create: bool) -> tuple[tuple[int, int, int, int], ...]:
    parts = components(path)
    descriptor = open_directory(parts, create=create)
    if exact_mode:
        assert_bound()
        # Retain the original descriptor across all candidates. Even a rename
        # immediately after this check cannot redirect chmod into a replacement.
        os.fchmod(descriptor, 0o700)
        previous = opened[parts][1]
        opened[parts] = descriptor, (previous[0], previous[1], 0o700, previous[3])
        os.fsync(descriptor)
    assert_bound()
    return tuple(opened[parts[:index]][1] for index in range(len(parts) + 1))


try:
    requested = sys.argv[1:]
    for candidate in requested:
        components(candidate)
    root_descriptor = os.open("/", flags)
    opened[()] = root_descriptor, directory_identity(os.fstat(root_descriptor), path="/")
    # Require the deployment-owned binary directory before normalizing any
    # directory, and retain its complete parent chain for the whole preflight.
    open_directory(components(requested[-1]), create=False)
    guards = {}
    for index, candidate in enumerate(requested):
        # The application binary directory is deployment-owned and must
        # already exist.  Its identity belongs in the same guard set as the
        # runtime directories that this script prepares.
        chain = prepare(candidate, exact_mode=index < 3, create=index < len(requested) - 1)
        if candidate in {requested[0], requested[1], requested[2], requested[-1]}:
            guards[candidate] = chain
    print(json.dumps(guards, sort_keys=True, separators=(",", ":")))
except (OSError, RuntimeError) as exc:
    raise SystemExit(f"TermDeck runtime directory preparation refused: {exc}") from exc
finally:
    for descriptor, _identity in reversed(tuple(opened.values())):
        os.close(descriptor)
PY
)

# Restores validate the addresses in authenticated stored generations, so
# recovery remains available even when the current LAN interface is down.
lan_ip=
if [ -z "$restore_generation" ] && [ "$restore_unreceipted" -eq 0 ]; then
    lan_ip=$(/usr/bin/ip -4 route get 1.1.1.1 2>/dev/null | /usr/bin/awk 'NR==1 {for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)
    if [ -z "$lan_ip" ]; then
        lan_ip=$(/usr/bin/ip -4 -o address show scope global | /usr/bin/awk 'NR==1 {split($4,a,"/"); print a[1]}')
    fi
    /usr/bin/python3 -I - "$lan_ip" <<'PY'
import ipaddress,sys
address=ipaddress.ip_address(sys.argv[1])
private=(ipaddress.ip_network('10.0.0.0/8'),ipaddress.ip_network('172.16.0.0/12'),ipaddress.ip_network('192.168.0.0/16'))
if address.version != 4 or not any(address in network for network in private):
    raise SystemExit(f"TermDeck refused non-RFC1918 primary IPv4 address: {address}")
PY
fi

# Bind the preflight chains before the existing TLS preparation performs any
# path-based work.  The publication helper repeats this binding immediately
# before output mutation; this early guard keeps a post-preflight TLS parent
# replacement from redirecting certificate rotation first.
verify_runtime_guards() {
    /usr/bin/python3 -I - "$runtime" "$runtime_guards" <<'PY'
import json
import os
import stat
import sys

directory = getattr(os, "O_DIRECTORY", 0)
nofollow = getattr(os, "O_NOFOLLOW", 0)
if not directory or not nofollow:
    raise SystemExit("TermDeck runtime directory guard requires O_DIRECTORY and O_NOFOLLOW")
flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def identity(info: os.stat_result, label: str) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise RuntimeError(f"not a real directory: {label}")
    return int(info.st_dev), int(info.st_ino), stat.S_IMODE(info.st_mode), int(info.st_uid)


def chain(path: str) -> tuple[tuple[int, int, int, int], ...]:
    if not path or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise RuntimeError(f"not an absolute canonical path: {path!r}")
    descriptor = os.open("/", flags)
    result = [identity(os.fstat(descriptor), "/")]
    try:
        for component in path.split("/")[1:]:
            if not component or component in {".", ".."} or "/" in component or "\\" in component:
                raise RuntimeError(f"unsafe path: {path!r}")
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            result.append(identity(os.fstat(descriptor), path))
        return tuple(result)
    finally:
        os.close(descriptor)


try:
    runtime = sys.argv[1]
    supplied = json.loads(sys.argv[2])
    paths = (runtime, runtime + "/nginx", runtime + "/tls", runtime + "/bin")
    if not isinstance(supplied, dict) or set(supplied) != set(paths):
        raise RuntimeError("invalid directory-preflight guard payload")
    for path in paths:
        expected = supplied[path]
        if (
            not isinstance(expected, list)
            or not expected
            or any(
                not isinstance(item, list)
                or len(item) != 4
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in item)
                or item[1] <= 0
                for item in expected
            )
            or tuple(tuple(item) for item in expected) != chain(path)
        ):
            raise RuntimeError(f"directory identity changed since preflight: {path}")
except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"TermDeck runtime directory guard refused: {exc}") from exc
PY
}

verify_runtime_guards

# Publish TLS as one descriptor-rooted generation exchange.  The manager emits
# refreshed full parent-chain guards only after it proves the exchanged TLS
# directory is the staged generation; its stdout is captured so the wrapper's
# normal CLI output remains unchanged.
tls_transaction() {
    local action=$1
    local work_name=${2:-}
    /usr/bin/python3 -I - "$action" "$runtime" "$runtime_guards" "$lan_ip" "$work_name" <<'PY'
from __future__ import annotations

import ctypes
import base64
import errno
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys


class TlsError(RuntimeError):
    pass


class NeedsRotation(RuntimeError):
    pass


class CompletedTlsDrift(NeedsRotation):
    """A signed terminal generation no longer matches its served leaves."""


class IncompleteTlsInventory(NeedsRotation):
    """A bootstrap TLS directory lacks one or more served leaves."""


_NOREPLACE = 1
_EXCHANGE = 2
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
if not _DIRECTORY or not _NOFOLLOW:
    raise SystemExit("TermDeck TLS transaction requires O_DIRECTORY and O_NOFOLLOW")
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_TARGETS = (
    ("server.key", 0o600),
    ("server.crt", 0o644),
    ("server-chain.crt", 0o644),
    ("server.ext", 0o600),
)
_STATIC = ("ca.crt", "ca.key", "ca.srl")
_JOURNAL_PREFIX = ".termdeck-tls-transaction-"
_CANDIDATE_PREFIX = ".termdeck-tls-candidate-"
_RECOVERY_KEY = ".termdeck-tls-recovery-key"
_SCHEMA = "termdeck.tls-directory-transaction.v1"
_READY = "ready"
_EXCHANGED = "exchanged"
_COMPLETE = "complete"
_INERT = "inert"
_ROLLBACK_READY = "rollback-ready"
_ROLLBACK_EXCHANGED = "rollback-exchanged"
_ROLLBACK_OUTPUTS_EXCHANGED = "rollback-outputs-exchanged"
_ROLLBACK_RECEIPT_RESTORED = "rollback-receipt-restored"
_ROLLBACK_COMPLETE = "rollback-complete"
_MARKER_AUTH_SUFFIX = ".auth"
_INTENT = "intent.json"
_RECEIPT = "build-receipt.json"
_PRIOR_RECEIPT = "prior-build-receipt.json"
_PRIOR_TLS = "prior-tls"
_PRIOR_CURRENT = "prior-current-lan-ip"
_PRIOR_HOSTS = "prior-allowed-hosts.map"
_ROTATION_EVIDENCE_PREFIX = "rotation-evidence-"
_ROLLBACK_PRIOR_RECEIPT = "rollback-prior-build-receipt.json"
_ROLLBACK_PRIOR_CURRENT = "rollback-prior-current-lan-ip"
_ROLLBACK_PRIOR_HOSTS = "rollback-prior-allowed-hosts.map"
_ROLLBACK_RESTORED_RECEIPT = "rollback-restored-build-receipt.json"
_ROLLBACK_RESTORED_RECEIPT_STAGING_PREFIX = "rollback-restored-build-receipt.json.staging-"
_RUNTIME_TRANSACTION_PREFIX = ".termdeck-runtime-transaction-"
_RECEIPT_JOURNAL_PREFIX = ".termdeck-receipt-transaction-"
_RECEIPT_JOURNAL_EVIDENCE = "receipt-finalization-journal"
_RECEIPT_JOURNAL_MODE = 0o600
_RECEIPT_JOURNAL_MAX_BYTES = 256 * 1024
_RUNTIME_RECEIPT_SCHEMA = "termdeck.lan-runtime-build.v2"
_RUNTIME_RECEIPT_DOMAIN = b"termdeck.lan-runtime-build.v2\x00"
_ATTEST_HELPER = "/usr/libexec/termdeck-preparer-state"
_RECEIPT_SOURCE_PATHS = {
    "bin/termdeck-web-server",
    "nginx/nginx.conf",
    "tls/ca.crt",
    "tls/server-chain.crt",
    "tls/server.crt",
}


def fail(message: str, exc: BaseException | None = None) -> TlsError:
    return TlsError(message if exc is None else f"{message}: {exc}")


def run_attestation_helper(
    receipt_path: str,
    *,
    transaction_id: str | None = None,
    label: str,
) -> None:
    """Use the fixed receipt helper without inheriting caller-controlled imports."""
    arguments = [_ATTEST_HELPER, "--receipt", receipt_path]
    if transaction_id is None:
        arguments.append("--verify-existing")
    else:
        if re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
            raise fail(f"{label} has an invalid attestation transaction")
        arguments.extend(("--transaction-id", transaction_id))
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            close_fds=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise fail(f"{label} attestation helper failed", exc) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise fail(
            f"{label} attestation helper refused the receipt"
            + (f": {detail}" if detail else "")
        )


def rfc1918_ipv4(value: object, *, label: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError) as exc:
        raise fail(f"{label} is not a private IPv4 address", exc) from exc
    private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if not isinstance(address, ipaddress.IPv4Address) or not any(address in network for network in private):
        raise fail(f"{label} is not a private IPv4 address")
    return address


def component(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise fail(f"unsafe {label} component: {value!r}")
    return value


def directory_identity(info: os.stat_result, *, label: str) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise fail(f"{label} is not a real directory")
    return int(info.st_dev), int(info.st_ino), stat.S_IMODE(info.st_mode), int(info.st_uid)


def file_identity(info: os.stat_result, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if info.st_ino <= 0 or info.st_nlink <= 0:
        raise fail(f"{label} has an unsupported filesystem identity")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def same_object(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    return left[:4] == right[:4]


def same_after_rename(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    return left[:6] == right[:6]


def fsync(descriptor: int, *, label: str) -> None:
    for attempt in range(3):
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            if attempt == 2:
                break
        except OSError as exc:
            raise fail(f"could not fsync {label}", exc) from exc
    raise fail(f"could not fsync {label}: interrupted three times")


def lstat_at(descriptor: int, name: str, *, label: str) -> os.stat_result | None:
    component(name, label=label)
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise fail(f"could not inspect {label}", exc) from exc


def safe_leaf(
    descriptor: int,
    name: str,
    *,
    label: str,
    required_mode: int | None = None,
) -> tuple[int, int, int, int, int, int, int] | None:
    info = lstat_at(descriptor, name, label=label)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise fail(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise fail(f"{label} is not a regular file")
    identity = file_identity(info, label=label)
    if identity[3] != 1:
        raise fail(f"{label} has unexpected hard links")
    if required_mode is not None and identity[2] != required_mode:
        raise fail(f"{label} has unsafe mode")
    return identity


def read_exact(
    descriptor: int,
    name: str,
    *,
    label: str,
    required_mode: int | None = None,
    limit: int = 32 * 1024 * 1024,
) -> tuple[bytes, tuple[int, int, int, int, int, int, int]]:
    before = safe_leaf(descriptor, name, label=label, required_mode=required_mode)
    if before is None:
        raise FileNotFoundError(name)
    try:
        opened = os.open(name, _READ_FLAGS, dir_fd=descriptor)
    except OSError as exc:
        raise fail(f"could not securely open {label}", exc) from exc
    try:
        current = file_identity(os.fstat(opened), label=label)
        if not same_object(current, before):
            raise fail(f"{label} changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(opened, 1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise fail(f"{label} exceeds size limit")
            chunks.append(block)
        after = file_identity(os.fstat(opened), label=label)
        named = safe_leaf(descriptor, name, label=label, required_mode=required_mode)
        if after != current or named != before:
            raise fail(f"{label} changed while reading")
        return b"".join(chunks), current
    finally:
        os.close(opened)


def open_readonly(
    descriptor: int,
    name: str,
    *,
    label: str,
    required_mode: int | None = None,
) -> int:
    """Open a stable regular leaf for an OpenSSL child without pathname trust."""
    expected = safe_leaf(descriptor, name, label=label, required_mode=required_mode)
    if expected is None:
        raise FileNotFoundError(name)
    try:
        opened = os.open(name, _READ_FLAGS, dir_fd=descriptor)
    except OSError as exc:
        raise fail(f"could not securely open {label}", exc) from exc
    try:
        observed = file_identity(os.fstat(opened), label=label)
        if not same_object(observed, expected):
            raise fail(f"{label} changed while opening")
        return opened
    except BaseException:
        os.close(opened)
        raise


def write_exclusive(
    descriptor: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int, int]:
    component(name, label=label)
    try:
        output = os.open(name, _WRITE_FLAGS, mode, dir_fd=descriptor)
    except OSError as exc:
        raise fail(f"could not create {label}", exc) from exc
    try:
        os.fchmod(output, mode)
        position = 0
        while position < len(data):
            written = os.write(output, data[position:])
            if written <= 0:
                raise fail(f"could not write {label}")
            position += written
        fsync(output, label=label)
        observed = file_identity(os.fstat(output), label=label)
        named = safe_leaf(descriptor, name, label=label, required_mode=mode)
        if (
            observed[2] != mode
            or observed[3] != 1
            or observed[4] != len(data)
            or named is None
            or not same_object(named, observed)
        ):
            raise fail(f"{label} changed while writing")
        fsync(descriptor, label=f"parent after writing {label}")
        return observed
    finally:
        os.close(output)


def absolute_components(path: str, *, label: str) -> tuple[str, ...]:
    if not path or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise fail(f"{label} is not an absolute canonical path: {path!r}")
    values = tuple(component(part, label=label) for part in path.split("/")[1:])
    if not values:
        raise fail(f"{label} must not be filesystem root")
    return values


class Anchor:
    def __init__(self, path: str, descriptor: int, chain: tuple[tuple[int, int, int, int], ...], label: str) -> None:
        self.path = path
        self.descriptor = descriptor
        self.chain = chain
        self.label = label

    def assert_current(self, *, phase: str) -> None:
        try:
            current = directory_identity(os.fstat(self.descriptor), label=self.label)
        except OSError as exc:
            raise fail(f"{self.label} descriptor unavailable {phase}", exc) from exc
        if current != self.chain[-1]:
            raise fail(f"{self.label} identity changed {phase}")
        fresh = open_anchor(self.path, label=self.label)
        try:
            if fresh.chain != self.chain:
                raise fail(f"{self.label} identity changed {phase}")
        finally:
            fresh.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass


def open_anchor(path: str, *, label: str) -> Anchor:
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise fail(f"could not open filesystem root for {label}", exc) from exc
    chain: list[tuple[int, int, int, int]] = []
    try:
        chain.append(directory_identity(os.fstat(descriptor), label="filesystem root"))
        for value in absolute_components(path, label=label):
            try:
                child = os.open(value, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise fail(f"{label} is not a real directory", exc) from exc
            os.close(descriptor)
            descriptor = child
            chain.append(directory_identity(os.fstat(descriptor), label=label))
        return Anchor(path, descriptor, tuple(chain), label)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


class Context:
    def __init__(self, runtime: Anchor, tls: Anchor, nginx: Anchor, binaries: Anchor) -> None:
        self.runtime = runtime
        self.tls = tls
        self.nginx = nginx
        self.binaries = binaries

    def anchors(self) -> tuple[Anchor, ...]:
        return self.runtime, self.tls, self.nginx, self.binaries

    def assert_all(self, *, phase: str) -> None:
        for anchor in self.anchors():
            anchor.assert_current(phase=phase)

    def rebind_tls(self, expected: tuple[int, int, int, int]) -> None:
        fresh = open_anchor(self.runtime.path + "/tls", label="TermDeck TLS runtime parent")
        try:
            if fresh.chain[-1] != expected:
                raise fail("TLS directory changed during secure exchange")
            self.tls.close()
            self.tls = fresh
            fresh = None  # ownership moved
        finally:
            if fresh is not None:
                fresh.close()

    def close(self) -> None:
        for anchor in reversed(self.anchors()):
            anchor.close()


def guard_chain(raw: object, *, label: str) -> tuple[tuple[int, int, int, int], ...]:
    if not isinstance(raw, list) or not raw:
        raise fail(f"invalid directory-preflight guard for {label}")
    result: list[tuple[int, int, int, int]] = []
    for value in raw:
        if (
            not isinstance(value, list)
            or len(value) != 4
            or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
            or value[1] <= 0
        ):
            raise fail(f"invalid directory-preflight guard for {label}")
        result.append((value[0], value[1], value[2], value[3]))
    return tuple(result)


def bind_guards(serialized: str, context: Context) -> None:
    expected = {
        context.runtime.path: context.runtime,
        context.runtime.path + "/tls": context.tls,
        context.runtime.path + "/nginx": context.nginx,
        context.runtime.path + "/bin": context.binaries,
    }
    try:
        supplied = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise fail("invalid directory-preflight guard payload", exc) from exc
    if not isinstance(supplied, dict) or set(supplied) != set(expected):
        raise fail("invalid directory-preflight guard payload")
    for path, anchor in expected.items():
        if guard_chain(supplied[path], label=anchor.label) != anchor.chain:
            raise fail(f"{anchor.label} changed since directory preflight")
    context.assert_all(phase="after binding directory-preflight guards")


def serialize_guards(context: Context) -> str:
    result = {
        context.runtime.path: [list(item) for item in context.runtime.chain],
        context.runtime.path + "/tls": [list(item) for item in context.tls.chain],
        context.runtime.path + "/nginx": [list(item) for item in context.nginx.chain],
        context.runtime.path + "/bin": [list(item) for item in context.binaries.chain],
    }
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recovery_key(control_parent: Anchor, context: Context) -> bytes:
    """Load the durable journal authenticator from the guarded runtime parent.

    The runtime directory itself is intentionally treated as hostile for
    rename-race purposes, so a pending journal cannot authorize itself.  The
    one-time key lives alongside (not inside) the runtime directory, under the
    already-anchored parent component.  It is created once with O_EXCL and is
    never replaced, deleted, or silently adopted after a collision.
    """
    owner = control_parent.chain[-1][3]

    def checked() -> os.stat_result | None:
        info = lstat_at(control_parent.descriptor, _RECOVERY_KEY, label="TLS recovery authenticator")
        if info is None:
            return None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != owner
            or info.st_nlink != 1
        ):
            raise fail("TLS recovery authenticator is not a private regular file")
        return info

    context.runtime.assert_current(phase="before loading TLS recovery authenticator")
    control_parent.assert_current(phase="before loading TLS recovery authenticator")
    info = checked()
    if info is None:
        key = secrets.token_bytes(32)
        context.runtime.assert_current(phase="immediately before creating TLS recovery authenticator")
        control_parent.assert_current(phase="immediately before creating TLS recovery authenticator")
        write_exclusive(
            control_parent.descriptor,
            _RECOVERY_KEY,
            key,
            0o600,
            label="TLS recovery authenticator",
        )
        info = checked()
        if info is None:
            raise fail("TLS recovery authenticator disappeared after creation")
    data, identity = read_exact(
        control_parent.descriptor,
        _RECOVERY_KEY,
        label="TLS recovery authenticator",
        required_mode=0o600,
        limit=64,
    )
    after = checked()
    if (
        after is None
        or file_identity(after, label="TLS recovery authenticator") != identity
        or len(data) != 32
    ):
        raise fail("TLS recovery authenticator changed while loading")
    control_parent.assert_current(phase="after loading TLS recovery authenticator")
    context.runtime.assert_current(phase="after loading TLS recovery authenticator")
    return data


def renameat2(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
    flags: int,
    *,
    label: str,
) -> None:
    component(source, label=label)
    component(destination, label=label)
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise fail("secure TLS transaction requires renameat2", exc) from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    failure = 0
    for attempt in range(3):
        result = rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flags)
        if result == 0:
            return
        failure = ctypes.get_errno()
        if failure != errno.EINTR:
            break
        if attempt == 2:
            raise fail(f"TLS rename {label} interrupted three times")
    if failure == errno.EEXIST:
        raise FileExistsError(failure, os.strerror(failure), destination)
    if failure == errno.ENOENT:
        raise FileNotFoundError(failure, os.strerror(failure), source)
    if failure in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
        raise fail("secure TLS transaction requires renameat2")
    raise fail(f"secure TLS rename failed for {label}", OSError(failure, os.strerror(failure)))


def rename_noreplace(src_fd: int, source: str, dst_fd: int, destination: str, *, label: str) -> None:
    renameat2(src_fd, source, dst_fd, destination, _NOREPLACE, label=label)


def rename_exchange(fd: int, left: str, right: str, *, label: str) -> None:
    renameat2(fd, left, fd, right, _EXCHANGE, label=label)


def rename_exchange_between(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
    *,
    label: str,
) -> None:
    renameat2(source_fd, source, destination_fd, destination, _EXCHANGE, label=label)


class PrivateDir:
    def __init__(self, parent: Anchor | "PrivateDir", name: str, descriptor: int, identity: tuple[int, int, int, int], label: str) -> None:
        self.parent = parent
        self.name = name
        self.descriptor = descriptor
        self.identity = identity
        self.label = label

    @property
    def owner(self) -> int:
        return self.parent.identity[3] if isinstance(self.parent, PrivateDir) else self.parent.chain[-1][3]

    def assert_current(self, *, phase: str) -> None:
        if isinstance(self.parent, PrivateDir):
            self.parent.assert_current(phase=phase)
        else:
            self.parent.assert_current(phase=phase)
        current = directory_identity(os.fstat(self.descriptor), label=self.label)
        named = lstat_at(self.parent.descriptor, self.name, label=self.label)
        if (
            current != self.identity
            or named is None
            or not stat.S_ISDIR(named.st_mode)
            or directory_identity(named, label=self.label) != self.identity
            or self.identity[2] != 0o700
            or self.identity[3] != self.owner
        ):
            raise fail(f"{self.label} changed {phase}")

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass


def open_private_dir(
    parent: Anchor | PrivateDir,
    name: str,
    *,
    label: str,
    expected: tuple[int, int, int, int] | None = None,
) -> PrivateDir:
    component(name, label=label)
    info = lstat_at(parent.descriptor, name, label=label)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise fail(f"{label} is not a private real directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.descriptor)
    except OSError as exc:
        raise fail(f"could not open {label}", exc) from exc
    identity = directory_identity(os.fstat(descriptor), label=label)
    result = PrivateDir(parent, name, descriptor, identity, label)
    try:
        result.assert_current(phase="while opening")
        if expected is not None and identity != expected:
            raise fail(f"{label} identity changed")
        return result
    except BaseException:
        result.close()
        raise


def create_private_dir(
    parent: Anchor | PrivateDir,
    prefix: str,
    *,
    label: str,
    context: Context,
    transaction_id: str | None = None,
) -> PrivateDir:
    for _ in range(32):
        name = prefix + (transaction_id if transaction_id is not None else secrets.token_hex(16))
        context.assert_all(phase=f"immediately before creating {label}")
        if isinstance(parent, PrivateDir):
            parent.assert_current(phase=f"immediately before creating {label}")
        else:
            parent.assert_current(phase=f"immediately before creating {label}")
        try:
            os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise fail(f"could not create {label}", exc) from exc
        result: PrivateDir | None = None
        try:
            result = open_private_dir(parent, name, label=label)
            fsync(parent.descriptor, label=f"parent after creating {label}")
            context.assert_all(phase=f"after creating {label}")
            result.assert_current(phase=f"after creating {label}")
            return result
        except BaseException:
            if result is not None:
                result.close()
            raise
    raise fail(f"could not allocate {label}")


def marker_auth_name(name: str) -> str:
    component(name, label="TLS journal marker")
    return name + _MARKER_AUTH_SUFFIX


def marker_authentication(control_key: bytes, journal: PrivateDir, name: str, content: bytes) -> bytes:
    payload = b"termdeck.tls-marker.v1\0" + journal.name.encode("ascii") + b"\0" + name.encode("ascii") + b"\0" + content
    return hmac.new(control_key, payload, hashlib.sha256).hexdigest().encode("ascii") + b"\n"


def marker(
    journal: PrivateDir,
    name: str,
    content: bytes,
    *,
    context: Context,
    control_key: bytes,
) -> None:
    context.assert_all(phase=f"before writing TLS journal {name}")
    journal.assert_current(phase=f"before writing {name}")
    authentication_name = marker_auth_name(name)
    authentication = marker_authentication(control_key, journal, name, content)
    existing_authentication = safe_leaf(
        journal.descriptor,
        authentication_name,
        label=f"TLS journal {authentication_name}",
        required_mode=0o600,
    )
    if existing_authentication is None:
        # Authenticate before making the raw marker visible.  A crash in this
        # interval leaves an inert private journal rather than a forged phase.
        write_exclusive(
            journal.descriptor,
            authentication_name,
            authentication,
            0o600,
            label=f"TLS journal {authentication_name}",
        )
        fsync(journal.descriptor, label=f"TLS journal after {authentication_name}")
    else:
        existing_data, _ = read_exact(
            journal.descriptor,
            authentication_name,
            label=f"TLS journal {authentication_name}",
            required_mode=0o600,
        )
        if not hmac.compare_digest(existing_data, authentication):
            raise fail(f"TLS journal {authentication_name} has foreign content")
    existing = safe_leaf(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o600)
    if existing is not None:
        data, _ = read_exact(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o600)
        if data == content:
            return
        raise fail(f"TLS journal {name} has foreign content")
    write_exclusive(journal.descriptor, name, content, 0o600, label=f"TLS journal {name}")
    fsync(journal.descriptor, label=f"TLS journal after {name}")
    context.assert_all(phase=f"after writing TLS journal {name}")
    journal.assert_current(phase=f"after writing {name}")


def identity_json(value: tuple[int, int, int, int] | tuple[int, ...] | None) -> list[int] | None:
    return None if value is None else list(value)


def parse_dir_identity(value: object, *, label: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[2] != 0o700
    ):
        raise fail(f"invalid TLS journal {label}")
    return tuple(value)  # type: ignore[return-value]


def parse_file_identity(value: object, *, label: str) -> tuple[int, int, int, int, int, int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 7
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[3] != 1
    ):
        raise fail(f"invalid TLS journal {label}")
    return tuple(value)  # type: ignore[return-value]


def ext_bytes(lan_ip: str) -> bytes:
    return (
        "[v3_req]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=@alt_names\n"
        "[alt_names]\n"
        "DNS.1=termdeck.local\n"
        "DNS.2=max.local\n"
        f"IP.1={lan_ip}\n"
        "IP.2=127.0.0.1\n"
    ).encode("ascii")


def prior_lan_ip_from_extension(directory: Anchor | PrivateDir) -> str:
    data, _ = read_exact(directory.descriptor, "server.ext", label="prior TLS server.ext", required_mode=0o600)
    match = re.search(rb"^IP\.1=([^\r\n]+)$", data, re.MULTILINE)
    if match is None:
        raise fail("prior TLS extension does not bind a LAN address")
    try:
        value = match.group(1).decode("ascii")
    except UnicodeDecodeError as exc:
        raise fail("prior TLS extension does not bind a LAN address", exc) from exc
    rfc1918_ipv4(value, label="prior TLS extension LAN address")
    return value


def openssl(arguments: list[str], descriptors: tuple[int, ...], *, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["/usr/bin/openssl", *arguments],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            pass_fds=descriptors,
        )
    except OSError as exc:
        raise fail("could not execute openssl", exc) from exc


def fd_path(fd: int) -> str:
    return f"/proc/self/fd/{fd}"


def validate_tls_directory(descriptor: int, lan_ip: str, *, label: str) -> None:
    try:
        ca, _ = read_exact(descriptor, "ca.crt", label=f"{label}/ca.crt")
        _ca_key, _ = read_exact(descriptor, "ca.key", label=f"{label}/ca.key")
    except FileNotFoundError as exc:
        raise fail("missing TermDeck LAN CA material", exc) from exc
    try:
        key, _ = read_exact(descriptor, "server.key", label=f"{label}/server.key", required_mode=0o600)
        cert, _ = read_exact(descriptor, "server.crt", label=f"{label}/server.crt", required_mode=0o644)
        chain, _ = read_exact(descriptor, "server-chain.crt", label=f"{label}/server-chain.crt", required_mode=0o644)
        ext, _ = read_exact(descriptor, "server.ext", label=f"{label}/server.ext", required_mode=0o600)
    except FileNotFoundError as exc:
        raise NeedsRotation("TLS bundle is incomplete") from exc
    if not ca or not _ca_key or chain != cert + ca or ext != ext_bytes(lan_ip):
        raise NeedsRotation("TLS bundle does not meet the expected chain or extension contract")
    fds: list[int] = []
    try:
        for name, mode in (("ca.crt", None), ("ca.key", None), ("server.key", 0o600), ("server.crt", 0o644)):
            identity = safe_leaf(descriptor, name, label=f"{label}/{name}", required_mode=mode)
            if identity is None:
                raise NeedsRotation("TLS bundle is incomplete")
            opened = os.open(name, _READ_FLAGS, dir_fd=descriptor)
            if not same_object(file_identity(os.fstat(opened), label=f"{label}/{name}"), identity):
                os.close(opened)
                raise fail(f"{label}/{name} changed while validating")
            fds.append(opened)
        ca_fd, ca_key_fd, key_fd, cert_fd = fds
        expiry = openssl(["x509", "-in", fd_path(cert_fd), "-noout", "-checkend", "2592000"], tuple(fds))
        verified = openssl(["verify", "-CAfile", fd_path(ca_fd), fd_path(cert_fd)], tuple(fds))
        key_public = openssl(["pkey", "-in", fd_path(key_fd), "-pubout", "-outform", "DER"], tuple(fds))
        cert_pem = openssl(["x509", "-in", fd_path(cert_fd), "-pubkey", "-noout"], tuple(fds))
        cert_public = openssl(["pkey", "-pubin", "-pubout", "-outform", "DER"], tuple(fds), input_data=cert_pem.stdout)
        ca_key_public = openssl(["pkey", "-in", fd_path(ca_key_fd), "-pubout", "-outform", "DER"], tuple(fds))
        ca_pem = openssl(["x509", "-in", fd_path(ca_fd), "-pubkey", "-noout"], tuple(fds))
        ca_public = openssl(["pkey", "-pubin", "-pubout", "-outform", "DER"], tuple(fds), input_data=ca_pem.stdout)
        san = openssl(["x509", "-in", fd_path(cert_fd), "-noout", "-ext", "subjectAltName"], tuple(fds))
        expected_san = [
            "DNS:termdeck.local",
            "DNS:max.local",
            f"IP Address:{lan_ip}",
            "IP Address:127.0.0.1",
        ]
        observed_san = re.findall(r"(?:DNS:[^,\s]+|IP Address:[^,\s]+)", san.stdout.decode("ascii", "replace"))
        if (
            expiry.returncode != 0
            or verified.returncode != 0
            or key_public.returncode != 0
            or cert_pem.returncode != 0
            or cert_public.returncode != 0
            or ca_key_public.returncode != 0
            or ca_pem.returncode != 0
            or ca_public.returncode != 0
            or san.returncode != 0
            or key_public.stdout != cert_public.stdout
            or ca_key_public.stdout != ca_public.stdout
            or observed_san != expected_san
        ):
            raise NeedsRotation("TLS certificate does not satisfy key, CA, expiry, or SAN contract")
        if hashlib.sha256(key).digest() == b"":
            raise NeedsRotation("TLS private key invalid")
    finally:
        for opened in reversed(fds):
            try:
                os.close(opened)
            except OSError:
                pass


def assert_tls_inventory(tls: Anchor) -> None:
    allowed = {name for name, _ in _TARGETS} | set(_STATIC)
    try:
        names = os.listdir(tls.descriptor)
    except OSError as exc:
        raise fail("could not list TLS runtime directory", exc) from exc
    for name in names:
        if name in allowed:
            safe_leaf(tls.descriptor, name, label=f"TLS {name}")
            continue
        if name.startswith(".rotate."):
            info = lstat_at(tls.descriptor, name, label=f"TLS legacy rotation evidence {name}")
            if (
                info is None
                or stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or directory_identity(info, label=f"TLS legacy rotation evidence {name}")[2] != 0o700
                or directory_identity(info, label=f"TLS legacy rotation evidence {name}")[3] != tls.chain[-1][3]
            ):
                raise fail(f"TLS legacy rotation evidence is unsafe: {name}")
            continue
        raise fail(f"TLS runtime contains an unrecognized entry: {name}")


def tls_inventory_manifest(directory: Anchor | PrivateDir) -> dict[str, dict[str, object]]:
    """Capture a descriptor-rooted complete TLS inventory without judging expiry.

    Prior generations may be intentionally stale (expired/SAN/key mismatch) at
    the moment a repair is staged.  Their exact leaves remain rollback evidence
    even though they cannot be reused as a served candidate.
    """
    directory.assert_current(phase="before reading TLS inventory manifest")
    required_modes = {name: mode for name, mode in _TARGETS}
    required_names = {"ca.crt", "ca.key", *required_modes}
    allowed_names = required_names | {"ca.srl"}
    try:
        names = set(os.listdir(directory.descriptor))
    except OSError as exc:
        raise fail("could not list TLS candidate", exc) from exc
    unexpected = names - allowed_names
    owner = directory.identity[3] if isinstance(directory, PrivateDir) else directory.chain[-1][3]
    for name in tuple(unexpected):
        if not name.startswith(".rotate."):
            continue
        info = lstat_at(directory.descriptor, name, label=f"TLS rotation evidence {name}")
        if (
            info is None
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or directory_identity(info, label=f"TLS rotation evidence {name}")[2] != 0o700
            or directory_identity(info, label=f"TLS rotation evidence {name}")[3] != owner
        ):
            raise fail(f"TLS rotation evidence is unsafe: {name}")
        unexpected.remove(name)
    if not required_names.issubset(names):
        raise IncompleteTlsInventory("TLS candidate inventory is incomplete")
    if unexpected:
        raise fail("TLS candidate inventory contains an unexpected entry")
    result: dict[str, dict[str, object]] = {}
    for name in sorted(names & allowed_names):
        mode = required_modes.get(name)
        data, identity = read_exact(
            directory.descriptor,
            name,
            label=f"TLS candidate {name}",
            required_mode=mode,
        )
        result[name] = {
            "identity": list(identity),
            "mode": identity[2],
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    directory.assert_current(phase="after reading TLS inventory manifest")
    return result


def candidate_manifest(directory: Anchor | PrivateDir, lan_ip: str) -> dict[str, dict[str, object]]:
    """Capture one fully valid candidate TLS generation by descriptor."""
    result = tls_inventory_manifest(directory)
    try:
        validate_tls_directory(directory.descriptor, lan_ip, label="TLS candidate")
    except NeedsRotation as exc:
        raise fail("TLS candidate does not meet the required certificate contract", exc) from exc
    observed = tls_inventory_manifest(directory)
    if observed != result:
        raise fail("TLS candidate changed while creating its manifest")
    return result


def verify_tls_inventory_manifest(
    directory: Anchor | PrivateDir,
    expected: dict[str, dict[str, object]],
) -> None:
    if tls_inventory_manifest(directory) != expected:
        raise fail("TLS inventory changed since its durable manifest")


def parse_candidate_manifest(value: object) -> dict[str, dict[str, object]]:
    required_modes = {name: mode for name, mode in _TARGETS}
    required_names = {"ca.crt", "ca.key", *required_modes}
    allowed_names = required_names | {"ca.srl"}
    if not isinstance(value, dict) or not required_names.issubset(value) or set(value) - allowed_names:
        raise fail("invalid TLS candidate manifest")
    result: dict[str, dict[str, object]] = {}
    for name in sorted(value):
        entry = value[name]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "sha256"}:
            raise fail("invalid TLS candidate manifest")
        identity = parse_file_identity(entry["identity"], label=f"candidate manifest {name}")
        mode = entry["mode"]
        digest = entry["sha256"]
        if (
            identity is None
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode != identity[2]
            or (name in required_modes and mode != required_modes[name])
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise fail("invalid TLS candidate manifest")
        result[name] = {"identity": list(identity), "mode": mode, "sha256": digest}
    return result


def verify_candidate_manifest(
    directory: Anchor | PrivateDir,
    expected: dict[str, dict[str, object]],
    lan_ip: str,
) -> None:
    observed = candidate_manifest(directory, lan_ip)
    if observed != expected:
        raise fail("TLS candidate changed since its durable manifest")


def candidate_manifest_drift(
    directory: Anchor,
    expected: dict[str, dict[str, object]],
) -> set[str]:
    """Return changed regular leaves without accepting unsafe replacements.

    This is used only for a completed generation.  Its signed manifest lets a
    changed served key/cert request a fresh generation instead of letting a
    later generic TLS validation silently reuse it.  Symlinks, hardlinks,
    non-regular entries, and inventory changes still fail closed.
    """
    assert_tls_inventory(directory)
    changed: set[str] = set()
    for name, item in expected.items():
        identity = item["identity"]
        digest = item["sha256"]
        assert isinstance(identity, list)
        assert isinstance(digest, str)
        current = safe_leaf(directory.descriptor, name, label=f"TLS completed candidate {name}")
        if current is None:
            changed.add(name)
            continue
        data, observed = read_exact(directory.descriptor, name, label=f"TLS completed candidate {name}")
        if list(observed) != identity or hashlib.sha256(data).hexdigest() != digest:
            changed.add(name)
    directory.assert_current(phase="after checking completed TLS manifest")
    return changed


def receipt_file_contract(
    receipt: object,
    *,
    allow_unsigned: bool = False,
) -> dict[str, object]:
    """Validate a completed v2 receipt, or a producer-owned recovery core."""
    required = {
        "bin/termdeck-web-server",
        "nginx/nginx.conf",
        "tls/ca.crt",
        "tls/server-chain.crt",
        "tls/server.crt",
    }
    if not isinstance(receipt, dict):
        raise fail("build-receipt.json is not a managed TermDeck runtime receipt")
    schema = receipt.get("schema")
    core = {
        "files", "https_port", "lan_ip", "ok", "runtime_publication",
        "schema", "tls_publication",
    }
    allowed_top_levels = [
        core | {"producer_attestation"},
        # Read-only upgrade adapter for the last HMAC-producing release.
        core | {"producer_commitment"},
    ]
    if allow_unsigned:
        allowed_top_levels.append(core)
    if (
        schema != _RUNTIME_RECEIPT_SCHEMA
        or not any(set(receipt) == allowed for allowed in allowed_top_levels)
        or receipt.get("ok") is not True
        or not isinstance(receipt.get("https_port"), int)
        or isinstance(receipt.get("https_port"), bool)
        or not 1 <= receipt["https_port"] <= 65535
        or not isinstance(receipt.get("lan_ip"), str)
        or not isinstance(receipt.get("files"), dict)
        or set(receipt["files"]) != required
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in receipt["files"].values()
        )
    ):
        raise fail("build-receipt.json is not a managed TermDeck runtime receipt")
    try:
        rfc1918_ipv4(receipt["lan_ip"], label="build receipt LAN address")
    except TlsError as exc:
        raise fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
    tls_publication_contract(receipt["tls_publication"])
    if "producer_commitment" in receipt:
        producer_commitment(receipt)
    elif "producer_attestation" in receipt:
        if not isinstance(receipt.get("producer_attestation"), dict):
            raise fail("build-receipt.json has an invalid producer attestation")
    elif not allow_unsigned:
        raise fail("build-receipt.json lacks a producer attestation")
    return receipt


def tls_publication_contract(value: object) -> dict[str, object]:
    """Validate the signed durable TLS forward/rollback provenance."""
    if not isinstance(value, dict) or set(value) != {"generation", "current", "prior"}:
        raise fail("build-receipt.json has invalid TLS publication binding")
    generation = value["generation"]
    current = value["current"]
    prior = value["prior"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise fail("build-receipt.json has invalid TLS publication generation")
    if not isinstance(current, dict) or set(current) != {"path", "identity", "manifest"}:
        raise fail("build-receipt.json has invalid current TLS publication")
    if current.get("path") != "tls":
        raise fail("build-receipt.json has invalid current TLS publication")
    current_identity = parse_dir_identity(current.get("identity"), label="current TLS identity")
    current_manifest = parse_candidate_manifest(current.get("manifest"))
    normalized: dict[str, object] = {
        "generation": generation,
        "current": {
            "path": "tls",
            "identity": list(current_identity),
            "manifest": current_manifest,
        },
        "prior": None,
    }
    # Bootstrap has no complete generation to restore.  Every later forward
    # exchange records the exact retained prior directory and manifest.
    if prior is None:
        return normalized
    if not isinstance(prior, dict) or set(prior) != {"journal", "journal_identity", "path", "identity", "manifest"}:
        raise fail("build-receipt.json has invalid prior TLS publication")
    journal = prior.get("journal")
    if not isinstance(journal, str) or journal != _JOURNAL_PREFIX + generation:
        raise fail("build-receipt.json has invalid prior TLS publication")
    if prior.get("path") != _PRIOR_TLS:
        raise fail("build-receipt.json has invalid prior TLS publication")
    journal_identity = parse_dir_identity(prior.get("journal_identity"), label="TLS journal identity")
    prior_identity = parse_dir_identity(prior.get("identity"), label="prior TLS identity")
    prior_manifest = parse_candidate_manifest(prior.get("manifest"))
    normalized["prior"] = {
        "journal": journal,
        "journal_identity": list(journal_identity),
        "path": _PRIOR_TLS,
        "identity": list(prior_identity),
        "manifest": prior_manifest,
    }
    return normalized


def publication_identity(value: object, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 7
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[3] != 1
    ):
        raise fail(f"build-receipt.json has invalid publication identity for {label}")
    return tuple(value)  # type: ignore[return-value]


def receipt_immutable_identity(value: object) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[2] != 0o644
        or value[3] != 1
    ):
        raise fail("build-receipt.json has an invalid producer receipt identity")
    return tuple(value)  # type: ignore[return-value]


def receipt_source_inputs(value: object, files: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or not isinstance(files, dict) or set(value) != _RECEIPT_SOURCE_PATHS:
        raise fail("build-receipt.json has invalid producer source inputs")
    parsed: dict[str, dict[str, object]] = {}
    for path in sorted(_RECEIPT_SOURCE_PATHS):
        entry = value[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "sha256"}:
            raise fail("build-receipt.json has invalid producer source inputs")
        identity = publication_identity(entry["identity"], label=path)
        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or digest != files[path]
        ):
            raise fail("build-receipt.json has invalid producer source inputs")
        parsed[path] = {"identity": list(identity), "sha256": digest}
    return parsed


def producer_commitment(receipt: dict[str, object]) -> dict[str, object]:
    commitment = receipt.get("producer_commitment")
    if not isinstance(commitment, dict) or set(commitment) != {"algorithm", "receipt_identity", "source_inputs", "mac"}:
        raise fail("build-receipt.json has an invalid producer commitment")
    if commitment.get("algorithm") != "hmac-sha256-v1":
        raise fail("build-receipt.json has an invalid producer commitment")
    identity = receipt_immutable_identity(commitment.get("receipt_identity"))
    inputs = receipt_source_inputs(commitment.get("source_inputs"), receipt.get("files"))
    mac = commitment.get("mac")
    if not isinstance(mac, str) or re.fullmatch(r"[0-9a-f]{64}", mac) is None:
        raise fail("build-receipt.json has an invalid producer commitment")
    return {
        "algorithm": "hmac-sha256-v1",
        "receipt_identity": list(identity),
        "source_inputs": inputs,
        "mac": mac,
    }


def producer_commitment_core(receipt: dict[str, object], commitment: dict[str, object]) -> dict[str, object]:
    core: dict[str, object] = {
        "files": receipt["files"],
        "https_port": receipt["https_port"],
        "lan_ip": receipt["lan_ip"],
        "ok": True,
        "receipt_identity": commitment["receipt_identity"],
        "runtime_publication": receipt["runtime_publication"],
        "schema": receipt["schema"],
        "source_inputs": commitment["source_inputs"],
    }
    # Transitional committed v2 receipts predate TLS rollback provenance.
    # Verify their original MAC exactly; all newly emitted receipts include it.
    if "tls_publication" in receipt:
        core["tls_publication"] = receipt["tls_publication"]
    return core


def producer_commitment_mac(key: bytes, receipt: dict[str, object], commitment: dict[str, object]) -> str:
    payload = json.dumps(
        producer_commitment_core(receipt, commitment),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, _RUNTIME_RECEIPT_DOMAIN + payload, hashlib.sha256).hexdigest()


def source_input_snapshot(
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
) -> dict[str, dict[str, object]]:
    if set(source_postimages) != _RECEIPT_SOURCE_PATHS:
        raise fail("invalid runtime receipt source snapshot")
    return {
        path: {"identity": list(source_postimages[path][0]), "sha256": source_postimages[path][1]}
        for path in sorted(_RECEIPT_SOURCE_PATHS)
    }


def capture_receipt_source_postimages(
    context: Context,
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str]]:
    required = {
        "bin/termdeck-web-server": (context.binaries, "termdeck-web-server"),
        "nginx/nginx.conf": (context.nginx, "nginx.conf"),
        "tls/ca.crt": (context.tls, "ca.crt"),
        "tls/server-chain.crt": (context.tls, "server-chain.crt"),
        "tls/server.crt": (context.tls, "server.crt"),
    }
    context.assert_all(phase="before capturing receipt source postimages for TLS exchange")
    captured: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] = {}
    for path in sorted(_RECEIPT_SOURCE_PATHS):
        anchor, name = required[path]
        data, identity = read_exact(anchor.descriptor, name, label=path)
        captured[path] = (identity, hashlib.sha256(data).hexdigest())
    context.assert_all(phase="after capturing receipt source postimages for TLS exchange")
    return captured


def verify_producer_commitment(
    receipt: dict[str, object],
    receipt_identity: tuple[int, int, int, int, int, int, int],
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    control_key: bytes,
    *,
    allow_stale_served_tls: bool = False,
) -> None:
    commitment = producer_commitment(receipt)
    immutable = receipt_immutable_identity(commitment["receipt_identity"])
    if receipt_identity[:4] != immutable or receipt_identity[2] != 0o644 or receipt_identity[3] != 1:
        raise fail("build-receipt.json does not match its producer receipt identity")
    observed_inputs = source_input_snapshot(source_postimages)
    committed_inputs = commitment["source_inputs"]
    if committed_inputs != observed_inputs:
        # During an authorized forward A-to-B preparation the already signed
        # A receipt still authenticates its historical source snapshot while
        # the executable/config leaves can already contain B.  Its MAC,
        # receipt inode, runtime outputs, and TLS generation remain mandatory;
        # B's current inputs are independently bound by the new attested core.
        if not allow_stale_served_tls:
            raise fail("build-receipt.json does not match its producer source inputs")
    mac = commitment["mac"]
    assert isinstance(mac, str)
    if not hmac.compare_digest(mac, producer_commitment_mac(control_key, receipt, commitment)):
        raise fail("build-receipt.json producer commitment authentication failed")


def verify_runtime_publication(receipt: dict[str, object], context: Context) -> None:
    """Bind v2 receipts to exact descriptor-observed map/current postimages."""
    if receipt["schema"] != "termdeck.lan-runtime-build.v2":
        raise fail("build-receipt.json lacks the v2 runtime-publication binding")
    publication = receipt.get("runtime_publication")
    if not isinstance(publication, dict) or set(publication) != {"generation", "outputs"}:
        raise fail("build-receipt.json has invalid runtime-publication binding")
    generation = publication["generation"]
    outputs = publication["outputs"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise fail("build-receipt.json has invalid runtime-publication generation")
    expected = {
        "current-lan-ip": (context.runtime, "current-lan-ip", 0o600),
        "nginx/allowed-hosts.map": (context.nginx, "allowed-hosts.map", 0o600),
    }
    if not isinstance(outputs, dict) or set(outputs) != set(expected):
        raise fail("build-receipt.json has invalid runtime-publication outputs")
    context.assert_all(phase="before validating receipt postimages for TLS exchange")
    for path, (anchor, name, mode) in expected.items():
        entry = outputs[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "path", "sha256"}:
            raise fail(f"build-receipt.json has invalid postimage entry for {path}")
        identity = publication_identity(entry["identity"], label=path)
        if (
            entry["path"] != path
            or entry["mode"] != mode
            or identity[2] != mode
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise fail(f"build-receipt.json has invalid postimage entry for {path}")
        observed = safe_leaf(anchor.descriptor, name, label=path, required_mode=mode)
        if observed != identity:
            raise fail(f"runtime publication postimage changed for {path}")
        data, after = read_exact(anchor.descriptor, name, label=path, required_mode=mode)
        if after != identity or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise fail(f"runtime publication postimage changed for {path}")
    context.assert_all(phase="after validating receipt postimages for TLS exchange")


def managed_receipt(
    context: Context,
    control_key: bytes,
    *,
    allow_stale_served_tls: bool = False,
) -> tuple[int, int, int, int, int, int, int] | None:
    context.assert_all(phase="before validating build receipt for TLS exchange")
    identity = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    if identity is None:
        return None
    raw, opened = read_exact(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    try:
        receipt = receipt_file_contract(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
    source_postimages = capture_receipt_source_postimages(context)
    source_matches = {
        relative: digest for relative, (_identity, digest) in source_postimages.items()
    } == receipt["files"]
    if not source_matches and not allow_stale_served_tls:
        raise fail("build-receipt.json does not match current runtime files")
    verify_runtime_publication(receipt, context)
    if "producer_attestation" in receipt:
        run_attestation_helper(
            context.runtime.path + "/" + _RECEIPT,
            label="build-receipt.json",
        )
    else:
        verify_producer_commitment(
            receipt,
            identity,
            source_postimages,
            control_key,
            allow_stale_served_tls=allow_stale_served_tls and not source_matches,
        )
    if opened != identity:
        raise fail("build-receipt.json changed while validating")
    context.assert_all(phase="after validating build receipt for TLS exchange")
    return identity


def _openssl_output(result: subprocess.CompletedProcess[bytes], *, label: str) -> bytes:
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise fail(f"could not generate {label}" + (f": {detail}" if detail else ""))
    return result.stdout


def generate_rotation_work(context: Context, lan_ip: str, transaction_id: str) -> PrivateDir:
    """Generate TLS bytes through stable descriptors before any public move.

    OpenSSL never receives a public destination pathname.  It emits each
    generated object to stdout or reads it from an inherited descriptor; the
    manager writes the resulting bytes with O_EXCL|O_NOFOLLOW into the private
    rotation directory.  A parent replacement can therefore only make the
    anchored identity checks fail, never redirect a key or certificate write.
    """
    context.assert_all(phase="before generating TLS rotation work")
    assert_tls_inventory(context.tls)
    work = create_private_dir(
        context.tls,
        ".rotate.",
        label="TLS rotation work",
        context=context,
        transaction_id=transaction_id,
    )
    try:
        extension = ext_bytes(lan_ip)
        write_exclusive(work.descriptor, "server.ext", extension, 0o600, label="TLS rotation work server.ext")
        key = _openssl_output(openssl(["genpkey", "-algorithm", "RSA"], ()), label="TLS private key")
        write_exclusive(work.descriptor, "server.key", key, 0o600, label="TLS rotation work server.key")
        key_fd = open_readonly(work.descriptor, "server.key", label="TLS rotation work server.key", required_mode=0o600)
        try:
            csr = _openssl_output(
                openssl(
                    ["req", "-new", "-key", fd_path(key_fd), "-subj", "/CN=termdeck.local"],
                    (key_fd,),
                ),
                label="TLS certificate request",
            )
        finally:
            os.close(key_fd)
        write_exclusive(work.descriptor, "server.csr", csr, 0o600, label="TLS rotation work server.csr")
        ca_fd = open_readonly(context.tls.descriptor, "ca.crt", label="TLS ca.crt")
        ca_key_fd = open_readonly(context.tls.descriptor, "ca.key", label="TLS ca.key")
        csr_fd = open_readonly(work.descriptor, "server.csr", label="TLS rotation work server.csr", required_mode=0o600)
        ext_fd = open_readonly(work.descriptor, "server.ext", label="TLS rotation work server.ext", required_mode=0o600)
        try:
            serial = secrets.token_hex(16)
            certificate = _openssl_output(
                openssl(
                    [
                        "x509",
                        "-req",
                        "-in",
                        fd_path(csr_fd),
                        "-CA",
                        fd_path(ca_fd),
                        "-CAkey",
                        fd_path(ca_key_fd),
                        "-set_serial",
                        "0x" + serial,
                        "-days",
                        "825",
                        "-sha256",
                        "-extfile",
                        fd_path(ext_fd),
                        "-extensions",
                        "v3_req",
                    ],
                    (ca_fd, ca_key_fd, csr_fd, ext_fd),
                ),
                label="TLS certificate",
            )
        finally:
            for descriptor in (ext_fd, csr_fd, ca_key_fd, ca_fd):
                os.close(descriptor)
        write_exclusive(work.descriptor, "server.crt", certificate, 0o644, label="TLS rotation work server.crt")
        ca, _ = read_exact(context.tls.descriptor, "ca.crt", label="TLS ca.crt")
        write_exclusive(
            work.descriptor,
            "server-chain.crt",
            certificate + ca,
            0o644,
            label="TLS rotation work server-chain.crt",
        )
        for name, mode in _TARGETS:
            identity = safe_leaf(work.descriptor, name, label=f"TLS rotation work {name}", required_mode=mode)
            if identity is None:
                raise fail(f"TLS rotation work did not create {name}")
        fsync(work.descriptor, label="TLS rotation work after generation")
        work.assert_current(phase="after generating TLS rotation work")
        context.assert_all(phase="after generating TLS rotation work")
        return work
    except BaseException:
        work.close()
        raise


def seed_candidate(
    context: Context,
    candidate: PrivateDir,
    work: PrivateDir,
    lan_ip: str,
) -> None:
    context.assert_all(phase="before seeding TLS candidate")
    candidate.assert_current(phase="before seeding TLS candidate")
    assert_tls_inventory(context.tls)
    work.assert_current(phase="before seeding TLS candidate")
    for name in _STATIC:
        current = safe_leaf(context.tls.descriptor, name, label=f"TLS {name}")
        if current is None:
            if name == "ca.srl":
                continue
            raise fail(f"missing TermDeck LAN CA material: {name}")
        data, identity = read_exact(context.tls.descriptor, name, label=f"TLS {name}")
        write_exclusive(candidate.descriptor, name, data, identity[2], label=f"TLS candidate {name}")
    for name, mode in _TARGETS:
        data, _ = read_exact(work.descriptor, name, label=f"TLS rotation work {name}", required_mode=mode)
        write_exclusive(candidate.descriptor, name, data, mode, label=f"TLS candidate {name}")
    fsync(candidate.descriptor, label="TLS candidate after seed")
    candidate.assert_current(phase="after seeding TLS candidate")
    work.assert_current(phase="after seeding TLS candidate")
    context.assert_all(phase="after seeding TLS candidate")
    validate_tls_directory(candidate.descriptor, lan_ip, label="TLS candidate")


def write_intent(
    journal: PrivateDir,
    candidate: PrivateDir,
    rotation_work: tuple[int, int, int, int],
    manifest: dict[str, dict[str, object]],
    previous_manifest: dict[str, dict[str, object]] | None,
    previous_lan_ip: str | None,
    tls_previous: tuple[int, int, int, int],
    receipt_previous: tuple[int, int, int, int, int, int, int] | None,
    lan_ip: str,
    *,
    context: Context,
    control_key: bytes,
) -> None:
    unsigned = {
        "candidate": candidate.name,
        "candidate_identity": identity_json(candidate.identity),
        "candidate_manifest": manifest,
        "id": journal.name[len(_JOURNAL_PREFIX):],
        "lan_ip": lan_ip,
        "receipt_previous": identity_json(receipt_previous),
        "rotation_work_identity": identity_json(rotation_work),
        "schema": _SCHEMA,
        "tls_previous": identity_json(tls_previous),
        **(
            {"tls_previous_manifest": previous_manifest, "tls_previous_lan_ip": previous_lan_ip}
            if previous_manifest is not None and previous_lan_ip is not None
            else ({"tls_previous_manifest": previous_manifest} if previous_manifest is not None else {})
        ),
    }
    encoded_unsigned = json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    intent = dict(unsigned)
    intent["auth"] = hmac.new(control_key, encoded_unsigned, hashlib.sha256).hexdigest()
    data = (json.dumps(intent, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    write_exclusive(journal.descriptor, _INTENT, data, 0o600, label="TLS exchange intent")
    fsync(journal.descriptor, label="TLS journal after intent")
    context.assert_all(phase="after TLS exchange intent")
    journal.assert_current(phase="after TLS exchange intent")
    candidate.assert_current(phase="after TLS exchange intent")


def parse_intent(journal: PrivateDir, control_key: bytes) -> dict[str, object]:
    data, _ = read_exact(journal.descriptor, _INTENT, label="TLS exchange intent", required_mode=0o600)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("invalid TLS exchange intent", exc) from exc
    required = {
        "auth",
        "candidate",
        "candidate_identity",
        "candidate_manifest",
        "id",
        "lan_ip",
        "receipt_previous",
        "rotation_work_identity",
        "schema",
        "tls_previous",
    }
    allowed = {
        frozenset(required),
        frozenset(required | {"tls_previous_manifest"}),
        frozenset(required | {"tls_previous_manifest", "tls_previous_lan_ip"}),
    }
    if not isinstance(value, dict) or frozenset(value) not in allowed or value.get("schema") != _SCHEMA:
        raise fail("invalid TLS exchange intent")
    if not isinstance(value["id"], str) or re.fullmatch(r"[0-9a-f]{32}", value["id"]) is None:
        raise fail("invalid TLS exchange intent")
    if journal.name != _JOURNAL_PREFIX + value["id"]:
        raise fail("TLS exchange intent does not bind its journal name")
    candidate = value["candidate"]
    if not isinstance(candidate, str) or candidate != _CANDIDATE_PREFIX + value["id"]:
        raise fail("invalid TLS exchange intent")
    auth = value["auth"]
    if not isinstance(auth, str) or re.fullmatch(r"[0-9a-f]{64}", auth) is None:
        raise fail("invalid TLS exchange intent")
    unsigned = dict(value)
    del unsigned["auth"]
    encoded_unsigned = json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_auth = hmac.new(control_key, encoded_unsigned, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(auth, expected_auth):
        raise fail("TLS exchange intent authentication failed")
    try:
        rfc1918_ipv4(value["lan_ip"], label="TLS exchange intent LAN address")
    except TlsError as exc:
        raise fail("invalid TLS exchange intent", exc) from exc
    value["candidate_identity"] = parse_dir_identity(value["candidate_identity"], label="candidate identity")
    value["rotation_work_identity"] = parse_dir_identity(value["rotation_work_identity"], label="rotation work identity")
    value["candidate_manifest"] = parse_candidate_manifest(value["candidate_manifest"])
    value["tls_previous"] = parse_dir_identity(value["tls_previous"], label="TLS previous identity")
    if "tls_previous_manifest" in value:
        value["tls_previous_manifest"] = parse_candidate_manifest(value["tls_previous_manifest"])
    if "tls_previous_lan_ip" in value:
        try:
            rfc1918_ipv4(value["tls_previous_lan_ip"], label="TLS previous LAN address")
        except TlsError as exc:
            raise fail("invalid TLS exchange intent", exc) from exc
    value["receipt_previous"] = parse_file_identity(value["receipt_previous"], label="receipt identity")
    return value


def entry_identity(info: os.stat_result, *, label: str) -> tuple[int, int, int, int]:
    if info.st_ino <= 0 or info.st_nlink <= 0:
        raise fail(f"{label} has an unsupported filesystem identity")
    return int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_nlink)


def restore_held_foreign_regular(
    source_parent: Anchor | PrivateDir,
    source_name: str,
    destination_parent: Anchor | PrivateDir,
    destination_name: str,
    *,
    context: Context,
    label: str,
) -> bool:
    """Preserve a moved foreign regular leaf without rereading its mutable name.

    Link the opened source descriptor directly to the proven-empty destination;
    a staging temporary would itself have a final replacement race.  The old
    private name remains as recovery evidence and no public name is replaced.
    """
    context.assert_all(phase=f"before restoring held {label}")
    source_parent.assert_current(phase=f"before restoring held {label}")
    destination_parent.assert_current(phase=f"before restoring held {label}")
    if lstat_at(destination_parent.descriptor, destination_name, label=label) is not None:
        return False
    source = safe_leaf(source_parent.descriptor, source_name, label=label)
    if source is None:
        return False
    try:
        descriptor = os.open(source_name, _READ_FLAGS, dir_fd=source_parent.descriptor)
    except OSError as exc:
        raise fail(f"could not hold {label}", exc) from exc
    try:
        held = file_identity(os.fstat(descriptor), label=label)
        if not same_object(held, source):
            return False
        context.assert_all(phase=f"immediately before restoring held {label}")
        source_parent.assert_current(phase=f"immediately before restoring held {label}")
        destination_parent.assert_current(phase=f"immediately before restoring held {label}")
        try:
            os.link(
                f"/proc/self/fd/{descriptor}",
                destination_name,
                dst_dir_fd=destination_parent.descriptor,
                follow_symlinks=True,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise fail(f"could not restore held {label}", exc) from exc
        restored_info = lstat_at(destination_parent.descriptor, destination_name, label=label)
        if (
            restored_info is None
            or stat.S_ISLNK(restored_info.st_mode)
            or not stat.S_ISREG(restored_info.st_mode)
            or not same_object(file_identity(restored_info, label=label), held)
        ):
            raise fail(f"held {label} changed during restoration")
        fsync(source_parent.descriptor, label=f"source parent after restoring held {label}")
        fsync(destination_parent.descriptor, label=f"destination parent after restoring held {label}")
        context.assert_all(phase=f"after restoring held {label}")
        return True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def restore_moved_foreign_entry(
    source_parent: Anchor | PrivateDir,
    source_name: str,
    destination_parent: Anchor | PrivateDir,
    destination_name: str,
    expected: tuple[int, int, int, int],
    *,
    label: str,
) -> None:
    """Put an unexpected rename source back only into a proven-empty name.

    This is deliberately a no-replace move.  When another writer has occupied
    the original name, both objects remain as evidence instead of choosing one
    to overwrite or deleting either one.
    """
    source_parent.assert_current(phase=f"before restoring {label}")
    destination_parent.assert_current(phase=f"before restoring {label}")
    source = lstat_at(source_parent.descriptor, source_name, label=label)
    destination = lstat_at(destination_parent.descriptor, destination_name, label=label)
    if source is None or entry_identity(source, label=label) != expected:
        raise fail(f"{label} changed before safe restoration")
    if destination is not None:
        raise fail(f"{label} original name was occupied before safe restoration")
    try:
        rename_noreplace(
            source_parent.descriptor,
            source_name,
            destination_parent.descriptor,
            destination_name,
            label=f"restore {label}",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail(f"could not safely restore {label}", exc) from exc
    restored = lstat_at(destination_parent.descriptor, destination_name, label=label)
    if restored is None or entry_identity(restored, label=label) != expected:
        raise fail(f"{label} changed during safe restoration")
    fsync(source_parent.descriptor, label=f"source parent after restoring {label}")
    fsync(destination_parent.descriptor, label=f"destination parent after restoring {label}")
    source_parent.assert_current(phase=f"after restoring {label}")
    destination_parent.assert_current(phase=f"after restoring {label}")


def ensure_receipt_absent(intent: dict[str, object], context: Context, journal: PrivateDir) -> None:
    expected = intent["receipt_previous"]
    assert expected is None or isinstance(expected, tuple)
    current = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    prior = safe_leaf(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}")
    if expected is None:
        if current is not None or prior is not None:
            raise fail("build receipt changed during TLS exchange")
        return
    if prior is not None:
        if current is not None or not same_after_rename(prior, expected):
            raise fail("build receipt changed during TLS exchange")
        return
    if current is None or not same_after_rename(current, expected):
        raise fail("build receipt changed during TLS exchange")
    context.assert_all(phase="immediately before invalidating build receipt for TLS exchange")
    journal.assert_current(phase="immediately before invalidating build receipt")
    try:
        rename_noreplace(context.runtime.descriptor, _RECEIPT, journal.descriptor, _PRIOR_RECEIPT, label="invalidate build receipt for TLS exchange")
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail("build receipt changed before TLS exchange", exc) from exc
    moved_info = lstat_at(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}")
    moved = file_identity(moved_info, label=f"TLS journal {_PRIOR_RECEIPT}") if moved_info is not None else None
    if (
        moved is None
        or moved_info is None
        or not stat.S_ISREG(moved_info.st_mode)
        or moved[3] != 1
        or not same_after_rename(moved, expected)
        or safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None
    ):
        if moved is not None:
            try:
                restore_held_foreign_regular(
                    journal,
                    _PRIOR_RECEIPT,
                    context.runtime,
                    _RECEIPT,
                    context=context,
                    label="foreign build receipt moved during TLS invalidation",
                )
            except BaseException as restore_exc:
                raise fail("build receipt changed during TLS exchange and could not be safely restored", restore_exc) from restore_exc
        raise fail("build receipt changed while invalidating TLS exchange")
    fsync(journal.descriptor, label="TLS journal after build receipt invalidation")
    fsync(context.runtime.descriptor, label="runtime after build receipt invalidation")
    context.assert_all(phase="after invalidating build receipt for TLS exchange")
    journal.assert_current(phase="after invalidating build receipt")


def child_identity(parent: Anchor | PrivateDir, name: str, *, label: str) -> tuple[int, int, int, int] | None:
    info = lstat_at(parent.descriptor, name, label=label)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise fail(f"{label} is not a real directory")
    return directory_identity(info, label=label)


def directory_entry_identity(info: os.stat_result | None, *, label: str) -> tuple[int, int, int, int] | None:
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return directory_identity(info, label=label)


def marker_present(journal: PrivateDir, name: str, content: bytes, control_key: bytes) -> bool:
    existing = safe_leaf(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o600)
    if existing is None:
        return False
    data, _ = read_exact(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o600)
    if data != content:
        raise fail(f"TLS journal {name} has foreign content")
    authentication_name = marker_auth_name(name)
    authentication_identity = safe_leaf(
        journal.descriptor,
        authentication_name,
        label=f"TLS journal {authentication_name}",
        required_mode=0o600,
    )
    if authentication_identity is None:
        # The writer always fsyncs the authenticator before exposing its raw
        # marker.  A raw marker without one therefore cannot be a recoverable
        # crash state; treating it as pending would let an untrusted writer
        # steer transaction recovery.
        raise fail(f"TLS journal {name} is not authenticated")
    authentication, _ = read_exact(
        journal.descriptor,
        authentication_name,
        label=f"TLS journal {authentication_name}",
        required_mode=0o600,
    )
    if not hmac.compare_digest(authentication, marker_authentication(control_key, journal, name, content)):
        raise fail(f"TLS journal {authentication_name} has foreign content")
    return True


def verify_complete_journal(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
) -> None:
    """Prove that a signed terminal journal still describes the public state.

    A durable ``complete`` marker is only a phase witness.  It is never by
    itself permission to skip recovery: the public TLS directory must still
    be the signed candidate, its complete manifest and certificate contract
    must still hold, and the old generation/receipt must remain under the
    journal.  This makes a stale or forged terminal marker fail closed.
    """
    candidate_name = intent["candidate"]
    candidate_identity = intent["candidate_identity"]
    manifest = intent["candidate_manifest"]
    old_identity = intent["tls_previous"]
    rotation_work_identity = intent["rotation_work_identity"]
    previous_manifest = intent.get("tls_previous_manifest")
    previous_receipt = intent["receipt_previous"]
    lan_ip = intent["lan_ip"]
    assert isinstance(candidate_name, str)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(manifest, dict)
    assert isinstance(old_identity, tuple)
    assert isinstance(rotation_work_identity, tuple)
    assert previous_manifest is None or isinstance(previous_manifest, dict)
    assert previous_receipt is None or isinstance(previous_receipt, tuple)
    assert isinstance(lan_ip, str)
    context.assert_all(phase="before validating completed TLS exchange")
    journal.assert_current(phase="before validating completed TLS exchange")
    if not (
        marker_present(journal, _READY, b"ready\n", control_key)
        and marker_present(journal, _EXCHANGED, b"exchanged\n", control_key)
        and marker_present(journal, _COMPLETE, b"complete\n", control_key)
    ):
        raise fail("completed TLS exchange is missing an authenticated phase marker")
    if context.tls.chain[-1] != candidate_identity:
        raise fail("completed TLS exchange no longer owns the public TLS directory")
    if child_identity(context.runtime, candidate_name, label="TLS candidate") is not None:
        raise fail("completed TLS exchange still has a public candidate name")
    if child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}") != old_identity:
        raise fail("completed TLS exchange no longer retains the prior TLS generation")
    if previous_manifest is not None:
        prior_tls = open_private_dir(
            journal,
            _PRIOR_TLS,
            label=f"TLS journal {_PRIOR_TLS}",
            expected=old_identity,
        )
        try:
            verify_tls_inventory_manifest(prior_tls, previous_manifest)
        finally:
            prior_tls.close()
    verify_relocated_rotation_evidence(intent, context, journal)
    prior_receipt = safe_leaf(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}")
    if previous_receipt is None:
        if prior_receipt is not None:
            raise fail("completed TLS exchange has an unexpected prior build receipt")
    elif prior_receipt is None or not same_after_rename(prior_receipt, previous_receipt):
        raise fail("completed TLS exchange no longer retains its prior build receipt")
    drifted = candidate_manifest_drift(context.tls, manifest)
    if drifted:
        # Never build a replacement from altered CA material.  Changes only
        # to served target leaves can be remediated by a fresh transaction
        # using the still-manifest-bound CA; the caller records that it must
        # rotate instead of treating this complete journal as current proof.
        static_drift = drifted & {"ca.crt", "ca.key", "ca.srl"}
        if static_drift:
            raise fail("completed TLS exchange trust material changed: " + ", ".join(sorted(static_drift)))
        raise CompletedTlsDrift("completed TLS exchange served leaves changed: " + ", ".join(sorted(drifted)))
    verify_candidate_manifest(context.tls, manifest, lan_ip)
    # The final runtime publisher may already have created the new receipt.
    # It must be a v2 receipt bound to this completed TLS generation; an
    # absent receipt is also valid while publication has not yet begun.
    current_receipt = managed_receipt(
        context,
        control_key,
        allow_stale_served_tls=True,
    )
    if current_receipt is not None:
        raw, observed = read_exact(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
        try:
            receipt = receipt_file_contract(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
        if receipt["schema"] != "termdeck.lan-runtime-build.v2" or observed != current_receipt:
            raise fail("completed TLS exchange has an invalid final build receipt")
        if "tls_publication" not in receipt:
            raise fail("completed TLS exchange receipt lacks TLS provenance")
        publication = tls_publication_contract(receipt["tls_publication"])
        current_publication = publication["current"]
        assert isinstance(current_publication, dict)
        if (
            publication["generation"] != intent["id"]
            or tuple(current_publication["identity"]) != candidate_identity
            or current_publication["manifest"] != manifest
        ):
            raise fail("completed TLS exchange receipt does not bind its current TLS generation")
        receipt_prior = publication["prior"]
        if previous_manifest is None:
            if receipt_prior is not None:
                raise fail("completed TLS exchange receipt has an unexpected prior TLS generation")
        else:
            if not isinstance(receipt_prior, dict) or (
                receipt_prior.get("journal") != journal.name
                or tuple(receipt_prior.get("journal_identity", ())) != journal.identity
                or receipt_prior.get("path") != _PRIOR_TLS
                or tuple(receipt_prior.get("identity", ())) != old_identity
                or receipt_prior.get("manifest") != previous_manifest
            ):
                raise fail("completed TLS exchange receipt does not bind its retained prior TLS generation")
    context.assert_all(phase="after validating completed TLS exchange")
    journal.assert_current(phase="after validating completed TLS exchange")


def current_tls_publication(context: Context, control_key: bytes) -> dict[str, object]:
    """Return the authenticated completed TLS generation backing this run.

    The journal name is a strict runtime-relative leaf and its identity is
    included in the receipt, so a copied receipt cannot authorize a foreign
    transaction directory.  A missing current completed journal requests one
    fresh descriptor-rooted rotation before receipt publication.
    """
    try:
        names = os.listdir(context.runtime.descriptor)
    except OSError as exc:
        raise fail("could not list TLS exchange journals for provenance", exc) from exc
    found: dict[str, object] | None = None
    for name in names:
        if not name.startswith(_JOURNAL_PREFIX):
            continue
        journal = open_private_dir(context.runtime, name, label="TLS exchange journal")
        try:
            intent = parse_intent(journal, control_key)
            candidate_identity = intent["candidate_identity"]
            assert isinstance(candidate_identity, tuple)
            if context.tls.chain[-1] != candidate_identity:
                continue
            if not marker_present(journal, _COMPLETE, b"complete\n", control_key):
                continue
            verify_complete_journal(intent, context, journal, control_key)
            generation = intent["id"]
            manifest = intent["candidate_manifest"]
            old_identity = intent["tls_previous"]
            previous_manifest = intent.get("tls_previous_manifest")
            assert isinstance(generation, str)
            assert isinstance(manifest, dict)
            assert isinstance(old_identity, tuple)
            assert previous_manifest is None or isinstance(previous_manifest, dict)
            prior: dict[str, object] | None = None
            if previous_manifest is not None:
                if child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}") != old_identity:
                    raise fail("completed TLS exchange no longer retains its prior generation")
                prior = {
                    "journal": journal.name,
                    "journal_identity": identity_json(journal.identity),
                    "path": _PRIOR_TLS,
                    "identity": identity_json(old_identity),
                    "manifest": previous_manifest,
                }
            publication = {
                "generation": generation,
                "current": {
                    "path": "tls",
                    "identity": identity_json(candidate_identity),
                    "manifest": manifest,
                },
                "prior": prior,
            }
            tls_publication_contract(publication)
            if found is not None:
                raise fail("multiple completed TLS journals own the public generation")
            found = publication
        finally:
            journal.close()
    if found is None:
        raise NeedsRotation("no authenticated TLS generation provenance exists")
    context.assert_all(phase="after collecting TLS receipt provenance")
    return found


def rotation_evidence_names(intent: dict[str, object]) -> tuple[str, str]:
    generation = intent["id"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise fail("TLS exchange intent has an invalid rotation-work generation")
    return ".rotate." + generation, _ROTATION_EVIDENCE_PREFIX + generation


def reject_unbound_rotation_evidence(directory: Anchor | PrivateDir, *, label: str) -> None:
    """A legacy work directory cannot safely be carried into an inverse.

    ``tls_inventory_manifest`` deliberately tolerates old work directories so
    a normal preparer can diagnose them.  A rollback generation is stricter:
    activation needs the exact signed TLS inventory after an inverse, and an
    unbound directory would make that inventory ambiguous.
    """
    try:
        names = os.listdir(directory.descriptor)
    except OSError as exc:
        raise fail(f"could not list {label}", exc) from exc
    lingering = sorted(name for name in names if name.startswith(".rotate."))
    if lingering:
        raise fail(f"{label} contains unbound TLS rotation evidence: {', '.join(lingering)}")


def verify_relocated_rotation_evidence(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
) -> None:
    """Prove the signed work directory cannot reappear in restored TLS."""
    old_identity = intent["tls_previous"]
    expected = intent["rotation_work_identity"]
    assert isinstance(old_identity, tuple)
    assert isinstance(expected, tuple)
    source_name, evidence_name = rotation_evidence_names(intent)
    if expected[3] != old_identity[3]:
        raise fail("TLS rotation work has an unexpected owner")
    prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=old_identity)
    try:
        if child_identity(prior, source_name, label="TLS rotation work") is not None:
            raise fail("TLS rotation work remains inside the retained TLS generation")
        reject_unbound_rotation_evidence(prior, label=f"TLS journal {_PRIOR_TLS}")
        evidence = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
        if evidence != expected:
            raise fail("TLS retained rotation evidence changed")
        retained = open_private_dir(
            journal,
            evidence_name,
            label="TLS retained rotation evidence",
            expected=expected,
        )
        retained.close()
        context.assert_all(phase="after validating retained TLS rotation evidence")
        journal.assert_current(phase="after validating retained TLS rotation evidence")
    finally:
        prior.close()


def relocate_rotation_evidence(intent: dict[str, object], context: Context, journal: PrivateDir) -> None:
    """Move the one signed work directory out of ``prior-tls`` atomically.

    The directory exchange intentionally carries the private work directory
    with A.  Keeping it there makes a later descriptor-exact inverse restore
    an inventory that no v2 receipt authorized.  The signed transaction binds
    this one directory's identity, so recovery can distinguish an interrupted
    move from a foreign legacy directory without globbing or deletion.
    """
    old_identity = intent["tls_previous"]
    expected = intent["rotation_work_identity"]
    assert isinstance(old_identity, tuple)
    assert isinstance(expected, tuple)
    source_name, evidence_name = rotation_evidence_names(intent)
    if expected[3] != old_identity[3]:
        raise fail("TLS rotation work has an unexpected owner")
    context.assert_all(phase="before relocating TLS rotation evidence")
    journal.assert_current(phase="before relocating TLS rotation evidence")
    prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=old_identity)
    try:
        source = child_identity(prior, source_name, label="TLS rotation work")
        destination = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
        if source is None and destination == expected:
            reject_unbound_rotation_evidence(prior, label=f"TLS journal {_PRIOR_TLS}")
            # The previous run may have died after the rename but before the
            # source-directory fsync.  Complete both durability fences before
            # permitting the terminal marker on this idempotent retry.
            fsync(prior.descriptor, label="prior TLS after resuming rotation-evidence relocation")
            fsync(journal.descriptor, label="TLS journal after resuming rotation-evidence relocation")
            context.assert_all(phase="after resuming TLS rotation evidence relocation")
            journal.assert_current(phase="after resuming TLS rotation evidence relocation")
            return
        if source != expected or destination is not None:
            raise fail("TLS rotation evidence has an ambiguous relocation topology")
        reject_unbound_rotation_evidence_except(prior, source_name, label=f"TLS journal {_PRIOR_TLS}")
        work = open_private_dir(prior, source_name, label="TLS rotation work", expected=expected)
        try:
            context.assert_all(phase="immediately before relocating TLS rotation evidence")
            journal.assert_current(phase="immediately before relocating TLS rotation evidence")
            prior.assert_current(phase="immediately before relocating TLS rotation evidence")
            work.assert_current(phase="immediately before relocating TLS rotation evidence")
            try:
                rename_noreplace(
                    prior.descriptor,
                    source_name,
                    journal.descriptor,
                    evidence_name,
                    label="relocate TLS rotation evidence",
                )
            except (FileExistsError, FileNotFoundError) as exc:
                raise fail("TLS rotation evidence changed before relocation", exc) from exc
        finally:
            work.close()
        moved = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
        if moved != expected or child_identity(prior, source_name, label="TLS rotation work") is not None:
            # A directory has no safe descriptor-link restoration path.  Keep
            # both names as durable evidence and require explicit recovery.
            raise fail("TLS rotation evidence changed during relocation")
        reject_unbound_rotation_evidence(prior, label=f"TLS journal {_PRIOR_TLS}")
        fsync(prior.descriptor, label="prior TLS after relocating rotation evidence")
        fsync(journal.descriptor, label="TLS journal after relocating rotation evidence")
        context.assert_all(phase="after relocating TLS rotation evidence")
        journal.assert_current(phase="after relocating TLS rotation evidence")
    finally:
        prior.close()


def verify_inverse_rotation_evidence(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
) -> None:
    """Prove the restored public A directory has no unbound work tree."""
    expected = intent["rotation_work_identity"]
    assert isinstance(expected, tuple)
    _source_name, evidence_name = rotation_evidence_names(intent)
    reject_unbound_rotation_evidence(context.tls, label="restored TLS runtime")
    evidence = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
    if evidence != expected:
        raise fail("TLS retained rotation evidence changed before inverse completion")
    retained = open_private_dir(
        journal,
        evidence_name,
        label="TLS retained rotation evidence",
        expected=expected,
    )
    retained.close()
    context.assert_all(phase="after validating restored TLS rotation evidence")
    journal.assert_current(phase="after validating restored TLS rotation evidence")


def reject_unbound_rotation_evidence_except(
    directory: Anchor | PrivateDir,
    allowed: str,
    *,
    label: str,
) -> None:
    try:
        names = os.listdir(directory.descriptor)
    except OSError as exc:
        raise fail(f"could not list {label}", exc) from exc
    lingering = sorted(name for name in names if name.startswith(".rotate.") and name != allowed)
    if lingering:
        raise fail(f"{label} contains unbound TLS rotation evidence: {', '.join(lingering)}")


def exchange_and_recover(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
) -> None:
    candidate_name = intent["candidate"]
    candidate_identity = intent["candidate_identity"]
    manifest = intent["candidate_manifest"]
    old_identity = intent["tls_previous"]
    lan_ip = intent["lan_ip"]
    assert isinstance(candidate_name, str)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(manifest, dict)
    assert isinstance(old_identity, tuple)
    assert isinstance(lan_ip, str)
    # Authenticate ready inside both fresh publish and retry paths before
    # invalidating a receipt.  `stage_exchange` calls this routine directly,
    # so relying only on pending-journal classification would leave a final
    # marker-deletion race before the first public mutation.
    if not marker_present(journal, _READY, b"ready\n", control_key):
        raise fail("authenticated TLS exchange intent is missing its ready marker")
    if marker_present(journal, _COMPLETE, b"complete\n", control_key):
        raise fail("completed TLS exchange cannot be resumed as pending")
    # A raw forged exchanged marker must fail while the old public receipt is
    # still intact rather than being able to trigger a recovery mutation.
    exchanged = marker_present(journal, _EXCHANGED, b"exchanged\n", control_key)
    ensure_receipt_absent(intent, context, journal)
    # Inspect both RENAME_EXCHANGE inputs without walking either one as a
    # directory.  A hostile candidate can be a symlink or regular file, and a
    # hostile final TLS entry may likewise be non-directory after an exchange.
    # The anchored runtime descriptor lets us recognize both cases and undo a
    # proven exchange before a path-based open could follow either object.
    current_info = lstat_at(context.runtime.descriptor, "tls", label="TLS public entry")
    current_identity = directory_entry_identity(current_info, label="TLS public entry")
    candidate_info = lstat_at(context.runtime.descriptor, candidate_name, label="TLS candidate")
    candidate_identity_now = directory_entry_identity(candidate_info, label="TLS candidate")
    prior_identity = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
    if (
        not exchanged
        and current_identity == old_identity
        and candidate_identity_now == candidate_identity
        and prior_identity is None
    ):
        candidate = open_private_dir(context.runtime, candidate_name, label="TLS candidate", expected=candidate_identity)
        try:
            context.assert_all(phase="immediately before TLS directory exchange")
            journal.assert_current(phase="immediately before TLS directory exchange")
            candidate.assert_current(phase="immediately before TLS directory exchange")
            verify_candidate_manifest(candidate, manifest, lan_ip)
            context.assert_all(phase="immediately before verified TLS directory exchange")
            journal.assert_current(phase="immediately before verified TLS directory exchange")
            candidate.assert_current(phase="immediately before verified TLS directory exchange")
            rename_exchange(context.runtime.descriptor, "tls", candidate_name, label="TLS directory exchange")
            fsync(context.runtime.descriptor, label="runtime after TLS directory exchange")
        finally:
            candidate.close()
        current_info = lstat_at(context.runtime.descriptor, "tls", label="TLS public entry")
        current_identity = directory_entry_identity(current_info, label="TLS public entry")
        candidate_info = lstat_at(context.runtime.descriptor, candidate_name, label="prior TLS candidate")
        candidate_identity_now = directory_entry_identity(candidate_info, label="prior TLS candidate")
        if current_identity != candidate_identity or candidate_identity_now != old_identity:
            # Both RENAME_EXCHANGE inputs can be replaced after their final
            # validation.  Their post-exchange layouts are indistinguishable:
            # reversing a foreign candidate replacement could republish it at
            # ``tls``.  Preserve both entries under their observed names and
            # fail closed; an absent receipt prevents adoption of this state.
            raise fail("TLS directory changed during exchange")
    if current_identity != candidate_identity:
        raise fail("TLS directory changed during exchange")
    # A crash can land after RENAME_EXCHANGE and its parent fsync but before
    # the immediate post-exchange check.  A foreign candidate replacement and
    # a foreign public-TLS replacement have indistinguishable postimages, so
    # recovery retains any unexpected object rather than guessing which name
    # should receive it.  The receipt remains absent and the operator retains
    # both generations as evidence.
    if (
        not exchanged
        and prior_identity is None
        and candidate_info is not None
        and candidate_identity_now != old_identity
    ):
        raise fail("TLS directory changed during exchange")
    # Bind the fresh TLS anchor before any post-exchange mutation.  It must be
    # the staged directory, never a path-walked foreign replacement.
    context.rebind_tls(candidate_identity)
    verify_candidate_manifest(context.tls, manifest, lan_ip)
    # Re-read through descriptor roots after the rebind; every later mutation
    # uses this fresh exact state.
    candidate_identity_now = child_identity(context.runtime, candidate_name, label="prior TLS candidate")
    prior_identity = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
    if not exchanged:
        if not (
            (candidate_identity_now == old_identity and prior_identity is None)
            or (candidate_identity_now is None and prior_identity == old_identity)
        ):
            raise fail("TLS exchange state is not recoverable")
        marker(journal, _EXCHANGED, b"exchanged\n", context=context, control_key=control_key)
    if candidate_identity_now == old_identity and prior_identity is None:
        prior_candidate = open_private_dir(context.runtime, candidate_name, label="prior TLS candidate", expected=old_identity)
        try:
            context.assert_all(phase="immediately before retaining prior TLS generation")
            journal.assert_current(phase="immediately before retaining prior TLS generation")
            prior_candidate.assert_current(phase="immediately before retaining prior TLS generation")
            try:
                rename_noreplace(context.runtime.descriptor, candidate_name, journal.descriptor, _PRIOR_TLS, label="retain prior TLS generation")
            except (FileExistsError, FileNotFoundError) as exc:
                raise fail("prior TLS generation changed before retention", exc) from exc
            moved_info = lstat_at(journal.descriptor, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
            moved = directory_entry_identity(moved_info, label=f"TLS journal {_PRIOR_TLS}")
            if moved != old_identity:
                # A directory cannot be linked from a held descriptor.  Do not
                # lstat-then-rename it back: that would re-publish a final
                # foreign replacement.  Retain both names as evidence and
                # leave the receipt absent for explicit operator recovery.
                raise fail("prior TLS generation changed during retention; retained ambiguous directory")
            fsync(journal.descriptor, label="TLS journal after retaining prior generation")
            fsync(context.runtime.descriptor, label="runtime after retaining prior generation")
        finally:
            prior_candidate.close()
        prior_identity = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
        candidate_identity_now = child_identity(context.runtime, candidate_name, label="TLS candidate")
    if candidate_identity_now is not None or prior_identity != old_identity:
        raise fail("prior TLS generation changed during exchange")
    # The private key-generation workspace travelled with A through the
    # directory exchange.  Retain its signed identity beside, never inside,
    # A before a complete journal can authorize a future inverse.
    relocate_rotation_evidence(intent, context, journal)
    # The fresh current TLS anchor must bind to the exchanged candidate rather
    # than treating a newly walked path as an untrusted baseline.
    context.rebind_tls(candidate_identity)
    context.assert_all(phase="after TLS directory exchange")
    journal.assert_current(phase="after TLS directory exchange")
    verify_candidate_manifest(context.tls, manifest, lan_ip)
    verify_relocated_rotation_evidence(intent, context, journal)
    marker(journal, _COMPLETE, b"complete\n", context=context, control_key=control_key)


def marker_auth_only_valid(
    journal: PrivateDir,
    name: str,
    content: bytes,
    control_key: bytes,
) -> bool:
    """Return whether only a valid durable marker authenticator is present."""
    raw = safe_leaf(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o600)
    auth_name = marker_auth_name(name)
    authenticated = safe_leaf(journal.descriptor, auth_name, label=f"TLS journal {auth_name}", required_mode=0o600)
    if raw is not None:
        return False
    if authenticated is None:
        return False
    data, _ = read_exact(journal.descriptor, auth_name, label=f"TLS journal {auth_name}", required_mode=0o600)
    if not hmac.compare_digest(data, marker_authentication(control_key, journal, name, content)):
        raise fail(f"TLS journal {auth_name} has foreign content")
    return True


def inert_pre_ready_intent(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
) -> bool:
    """Prove an intent-only crash made no public mutation.

    ``intent.json`` is written before ``ready``.  Its HMAC makes the expected
    old TLS, candidate, and receipt identities authoritative, but a runtime
    writer can delete both ready witnesses after a later mutation.  Treat the
    journal as inert only when the complete signed pre-mutation topology is
    still descriptor-observable; any missing or substituted object remains
    ambiguous and fails closed.
    """
    candidate_name = intent["candidate"]
    candidate_identity = intent["candidate_identity"]
    manifest = intent["candidate_manifest"]
    old_identity = intent["tls_previous"]
    rotation_work_identity = intent["rotation_work_identity"]
    previous_receipt = intent["receipt_previous"]
    lan_ip = intent["lan_ip"]
    assert isinstance(candidate_name, str)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(manifest, dict)
    assert isinstance(old_identity, tuple)
    assert isinstance(rotation_work_identity, tuple)
    assert previous_receipt is None or isinstance(previous_receipt, tuple)
    assert isinstance(lan_ip, str)
    context.assert_all(phase="before classifying pre-ready TLS intent")
    journal.assert_current(phase="before classifying pre-ready TLS intent")
    # The ready authenticator is written and fsynced before raw ``ready``.
    # A kill in precisely that interval is still pre-mutation, but only a
    # valid HMAC authenticator may classify it as inert.  Every later phase
    # follows a public mutation, so even an auth-only witness remains
    # ambiguous and fails closed.
    ready = safe_leaf(journal.descriptor, _READY, label=f"TLS journal {_READY}", required_mode=0o600)
    if ready is not None:
        return False
    # Both ready.auth and inert.auth are written before their raw markers.
    # They are harmless only when their exact HMAC proves our own interrupted
    # write and the signed pre-mutation topology below still holds.
    marker_auth_only_valid(journal, _READY, b"ready\n", control_key)
    inert = safe_leaf(journal.descriptor, _INERT, label=f"TLS journal {_INERT}", required_mode=0o600)
    if inert is not None:
        return False
    marker_auth_only_valid(journal, _INERT, b"inert\n", control_key)
    for phase in (_EXCHANGED, _COMPLETE):
        if (
            safe_leaf(journal.descriptor, phase, label=f"TLS journal {phase}", required_mode=0o600) is not None
            or safe_leaf(
                journal.descriptor,
                marker_auth_name(phase),
                label=f"TLS journal {marker_auth_name(phase)}",
                required_mode=0o600,
            ) is not None
        ):
            return False
    if context.tls.chain[-1] != old_identity:
        return False
    if child_identity(context.runtime, candidate_name, label="TLS candidate") != candidate_identity:
        return False
    rotation_work_name, evidence_name = rotation_evidence_names(intent)
    rotation_work = child_identity(context.tls, rotation_work_name, label="TLS rotation work")
    retained_work = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
    if not (
        (rotation_work == rotation_work_identity and retained_work is None)
        or (rotation_work is None and retained_work == rotation_work_identity)
    ):
        return False
    if child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}") is not None:
        return False
    if safe_leaf(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}") is not None:
        return False
    receipt = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    if receipt != previous_receipt:
        return False
    candidate = open_private_dir(
        context.runtime,
        candidate_name,
        label="TLS candidate",
        expected=candidate_identity,
    )
    try:
        verify_candidate_manifest(candidate, manifest, lan_ip)
    finally:
        candidate.close()
    context.assert_all(phase="after classifying pre-ready TLS intent")
    journal.assert_current(phase="after classifying pre-ready TLS intent")
    return True


def retain_inert_rotation_evidence(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
) -> None:
    """Keep pre-ready work outside the TLS inventory before recording inert.

    The signed work identity authorizes exactly one no-replace relocation.
    Both its original and relocated topologies are recoverable after a kill;
    an unrelated directory is never removed or adopted as rotation evidence.
    """
    expected = intent["rotation_work_identity"]
    old_identity = intent["tls_previous"]
    assert isinstance(expected, tuple)
    assert isinstance(old_identity, tuple)
    source_name, evidence_name = rotation_evidence_names(intent)
    context.assert_all(phase="before retaining inert TLS rotation evidence")
    journal.assert_current(phase="before retaining inert TLS rotation evidence")
    retained = child_identity(journal, evidence_name, label="TLS retained rotation evidence")
    if context.tls.chain[-1] != old_identity:
        if retained != expected:
            raise fail("inert TLS rotation evidence no longer has its signed identity")
        return
    source = child_identity(context.tls, source_name, label="TLS rotation work")
    if source == expected and retained is None:
        work = open_private_dir(context.tls, source_name, label="TLS rotation work", expected=expected)
        try:
            context.assert_all(phase="immediately before retaining inert TLS rotation evidence")
            journal.assert_current(phase="immediately before retaining inert TLS rotation evidence")
            work.assert_current(phase="immediately before retaining inert TLS rotation evidence")
            try:
                rename_noreplace(
                    context.tls.descriptor,
                    source_name,
                    journal.descriptor,
                    evidence_name,
                    label="retain inert TLS rotation evidence",
                )
            except (FileExistsError, FileNotFoundError) as exc:
                raise fail("inert TLS rotation evidence changed before retention", exc) from exc
        finally:
            work.close()
    if (
        child_identity(context.tls, source_name, label="TLS rotation work") is not None
        or child_identity(journal, evidence_name, label="TLS retained rotation evidence") != expected
    ):
        raise fail("inert TLS rotation evidence has an ambiguous retention topology")
    # A retry may observe the rename before either parent was fsynced.
    fsync(context.tls.descriptor, label="TLS after retaining inert rotation evidence")
    fsync(journal.descriptor, label="TLS journal after retaining inert rotation evidence")
    context.assert_all(phase="after retaining inert TLS rotation evidence")
    journal.assert_current(phase="after retaining inert TLS rotation evidence")


def pending_journals(context: Context, control_key: bytes) -> tuple[list[PrivateDir], bool]:
    try:
        names = os.listdir(context.runtime.descriptor)
    except OSError as exc:
        raise fail("could not list TLS exchange journals", exc) from exc
    pending: list[PrivateDir] = []
    requires_rotation = False
    for name in names:
        if not name.startswith(_JOURNAL_PREFIX):
            continue
        journal = open_private_dir(context.runtime, name, label="TLS exchange journal")
        try:
            # Authenticate the immutable transaction intent before consulting
            # a mutable phase witness.  A runtime-directory writer can delete
            # both ready witnesses after receipt invalidation, so an intent
            # without ready is inert only if every signed pre-mutation object
            # is still exactly present.
            intent = parse_intent(journal, control_key)
            if marker_present(journal, _INERT, b"inert\n", control_key):
                # A prior retry durably classified this signed intent as
                # pre-mutation.  Keep the journal and candidate as evidence;
                # do not reclassify it after a later successful generation.
                retain_inert_rotation_evidence(intent, context, journal)
                continue
            ready = marker_present(journal, _READY, b"ready\n", control_key)
            if not ready:
                if inert_pre_ready_intent(intent, context, journal, control_key):
                    retain_inert_rotation_evidence(intent, context, journal)
                    marker(journal, _INERT, b"inert\n", context=context, control_key=control_key)
                    continue
                raise fail("authenticated TLS exchange intent is missing its ready marker")
            if marker_present(journal, _COMPLETE, b"complete\n", control_key):
                # Completed journals are durable historical evidence.  Only
                # the one whose signed candidate is the current public TLS
                # generation can authorize skipping recovery; older complete
                # journals naturally stop matching after later rotations.
                candidate_identity = intent["candidate_identity"]
                assert isinstance(candidate_identity, tuple)
                if context.tls.chain[-1] == candidate_identity:
                    try:
                        verify_complete_journal(intent, context, journal, control_key)
                    except CompletedTlsDrift:
                        # The journal remains authenticated evidence, but its
                        # target leaves can no longer authorize reuse.  Let
                        # the caller create a fresh transaction from the
                        # manifest-bound CA instead of failing before normal
                        # mismatch remediation can begin.
                        requires_rotation = True
                continue
            pending.append(journal)
            journal = None  # ownership transfers
        finally:
            if journal is not None:
                journal.close()
    return pending, requires_rotation


def recover_pending(context: Context, control_key: bytes) -> bool:
    journals, requires_rotation = pending_journals(context, control_key)
    try:
        if len(journals) > 1:
            raise fail("multiple pending TLS exchanges require operator recovery")
        if journals:
            journal = journals[0]
            exchange_and_recover(parse_intent(journal, control_key), context, journal, control_key)
            # The recovered candidate is freshly manifest-proved, so any
            # historical complete-journal drift no longer requests another
            # unnecessary rotation in this invocation.
            return False
        return requires_rotation
    finally:
        for journal in journals:
            journal.close()


def stage_exchange(context: Context, lan_ip: str, control_key: bytes) -> None:
    context.assert_all(phase="before TLS exchange preparation")
    assert_tls_inventory(context.tls)
    # A prior, unbound work directory would survive a directory inverse and
    # make the v2 TLS inventory differ from its signed manifest.  Do not
    # silently fold legacy evidence into a new rollback generation.
    reject_unbound_rotation_evidence(context.tls, label="TLS runtime")
    old_identity = context.tls.chain[-1]
    # Preserve a stale but complete prior generation as exact rollback
    # evidence.  Bootstrap is the only case with no prior full inventory.
    try:
        previous_manifest = tls_inventory_manifest(context.tls)
        previous_lan_ip = prior_lan_ip_from_extension(context.tls)
    except IncompleteTlsInventory:
        previous_manifest = None
        previous_lan_ip = None
    transaction_id = secrets.token_hex(16)
    work = generate_rotation_work(context, lan_ip, transaction_id)
    journal: PrivateDir | None = None
    candidate: PrivateDir | None = None
    try:
        journal = create_private_dir(
            context.runtime,
            _JOURNAL_PREFIX,
            label="TLS exchange journal",
            context=context,
            transaction_id=transaction_id,
        )
        candidate = create_private_dir(
            context.runtime,
            _CANDIDATE_PREFIX,
            label="TLS candidate",
            context=context,
            transaction_id=transaction_id,
        )
        seed_candidate(context, candidate, work, lan_ip)
        manifest = candidate_manifest(candidate, lan_ip)
        # The work directory remains named under the old TLS generation.  It
        # is no longer mutated and will travel atomically into the retained
        # prior generation when the directory exchange completes.
        work.close()
        # A previously final, HMAC-authenticated receipt may be stale solely
        # because the served cert/chain was deliberately replaced.  The next
        # exchange must move that exact old receipt into its journal before
        # publishing a repaired TLS generation; no other source drift is
        # accepted as a migration input.
        receipt_previous = managed_receipt(
            context,
            control_key,
            allow_stale_served_tls=True,
        )
        write_intent(
            journal,
            candidate,
            work.identity,
            manifest,
            previous_manifest,
            previous_lan_ip,
            old_identity,
            receipt_previous,
            lan_ip,
            context=context,
            control_key=control_key,
        )
        marker(journal, _READY, b"ready\n", context=context, control_key=control_key)
        exchange_and_recover(parse_intent(journal, control_key), context, journal, control_key)
    finally:
        work.close()
        if candidate is not None:
            candidate.close()
        if journal is not None:
            journal.close()


def _restore_result(
    generation: str,
    restored_identity: tuple[int, int, int, int],
    restored_manifest: dict[str, dict[str, object]],
    retained_identity: tuple[int, int, int, int],
    retained_manifest: dict[str, dict[str, object]],
    journal: PrivateDir,
) -> str:
    return json.dumps(
        {
            "action": "restored-tls-generation",
            "generation": generation,
            "ok": True,
            "receipt": "absent",
            "restored": {
                "identity": identity_json(restored_identity),
                "manifest": restored_manifest,
                "path": "tls",
            },
            "retained": {
                "identity": identity_json(retained_identity),
                "journal": journal.name,
                "journal_identity": identity_json(journal.identity),
                "manifest": retained_manifest,
                "path": _PRIOR_TLS,
            },
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def runtime_output_expectations(
    receipt: dict[str, object],
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str]]:
    """Parse the two leaf identities a strict v2 receipt owns."""
    publication = receipt.get("runtime_publication")
    if not isinstance(publication, dict) or set(publication) != {"generation", "outputs"}:
        raise fail("build-receipt.json has invalid runtime-publication binding")
    generation = publication.get("generation")
    outputs = publication.get("outputs")
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise fail("build-receipt.json has invalid runtime-publication generation")
    expected_modes = {
        "current-lan-ip": 0o600,
        "nginx/allowed-hosts.map": 0o600,
    }
    if not isinstance(outputs, dict) or set(outputs) != set(expected_modes):
        raise fail("build-receipt.json has invalid runtime-publication outputs")
    parsed: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] = {}
    for path, mode in expected_modes.items():
        entry = outputs[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "path", "sha256"}:
            raise fail(f"build-receipt.json has invalid postimage entry for {path}")
        identity = publication_identity(entry.get("identity"), label=path)
        digest = entry.get("sha256")
        if (
            entry.get("path") != path
            or entry.get("mode") != mode
            or identity[2] != mode
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise fail(f"build-receipt.json has invalid postimage entry for {path}")
        parsed[path] = (identity, digest)
    return parsed


def read_authenticated_runtime_receipt(
    parent: Anchor | PrivateDir,
    name: str,
    control_key: bytes,
    *,
    label: str,
    allow_unattested: bool = False,
) -> tuple[
    dict[str, object],
    tuple[int, int, int, int, int, int, int],
    dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    dict[str, object],
]:
    """Read a receipt without treating its current postimages as trusted.

    During an inverse the signed A receipt lives in the journal while B is
    public, so ``managed_receipt`` cannot validate A's live source paths yet.
    This helper proves the immutable receipt leaf and strict schema, then
    authenticates either its Ed25519 attestation or the read-only legacy HMAC
    upgrade format.  The caller validates postimages only in its own phase.
    """
    identity = safe_leaf(parent.descriptor, name, label=label, required_mode=0o644)
    if identity is None:
        raise fail(f"missing {label}")
    raw, opened = read_exact(parent.descriptor, name, label=label, required_mode=0o644)
    if opened != identity:
        raise fail(f"{label} changed while reading")
    try:
        receipt = receipt_file_contract(
            json.loads(raw.decode("utf-8")),
            allow_unsigned=allow_unattested,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail(f"{label} is not a managed TermDeck runtime receipt", exc) from exc
    if isinstance(parent, Anchor):
        receipt_path = parent.path + "/" + name
    elif isinstance(parent.parent, Anchor):
        receipt_path = parent.parent.path + "/" + parent.name + "/" + name
    else:
        raise fail(f"{label} is outside the supported receipt journal depth")
    if "producer_attestation" in receipt:
        run_attestation_helper(receipt_path, label=label)
        attested_identity = receipt["producer_attestation"].get("receipt_core_identity")
        if (
            not isinstance(attested_identity, list)
            or len(attested_identity) != 7
            or tuple(attested_identity[:4]) != identity[:4]
        ):
            raise fail(f"{label} does not match its attested receipt identity")
    elif "producer_commitment" in receipt:
        commitment = producer_commitment(receipt)
        immutable = receipt_immutable_identity(commitment["receipt_identity"])
        if identity[:4] != immutable:
            raise fail(f"{label} does not match its committed receipt identity")
        mac = commitment["mac"]
        assert isinstance(mac, str)
        if not hmac.compare_digest(mac, producer_commitment_mac(control_key, receipt, commitment)):
            raise fail(f"{label} producer commitment authentication failed")
    elif not allow_unattested:
        raise fail(f"{label} lacks a producer attestation")
    outputs = runtime_output_expectations(receipt)
    publication = tls_publication_contract(receipt["tls_publication"])
    return receipt, identity, outputs, publication


def leaf_matches(
    parent: Anchor | PrivateDir,
    name: str,
    expected: tuple[int, int, int, int, int, int, int],
    digest: str,
    *,
    label: str,
    allow_renamed: bool = False,
) -> bool:
    observed = safe_leaf(parent.descriptor, name, label=label, required_mode=expected[2])
    if observed is None or (not same_after_rename(observed, expected) if allow_renamed else observed != expected):
        return False
    data, opened = read_exact(parent.descriptor, name, label=label, required_mode=expected[2])
    if (
        (not same_after_rename(opened, expected) if allow_renamed else opened != expected)
        or hashlib.sha256(data).hexdigest() != digest
    ):
        raise fail(f"{label} changed while verifying its committed postimage")
    return True


def _read_receipt_journal_descriptor(descriptor: int, *, label: str) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise fail(f"{label} is not seekable", exc) from exc
    chunks: list[bytes] = []
    remaining = _RECEIPT_JOURNAL_MAX_BYTES + 1
    while remaining:
        try:
            block = os.read(descriptor, min(65536, remaining))
        except InterruptedError:
            continue
        except OSError as exc:
            raise fail(f"could not read {label}", exc) from exc
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    result = b"".join(chunks)
    if len(result) > _RECEIPT_JOURNAL_MAX_BYTES:
        raise fail(f"{label} exceeds its bounded size")
    return result


def _open_receipt_finalization_journal(
    parent: Anchor | PrivateDir,
    name: str,
    expected_receipt: tuple[int, int, int, int, int, int, int],
) -> dict[str, object] | None:
    """Open one preparer journal and bind its core to the failed receipt FD."""
    observed = safe_leaf(
        parent.descriptor,
        name,
        label="receipt finalization journal",
        required_mode=_RECEIPT_JOURNAL_MODE,
    )
    if observed is None:
        return None
    info = lstat_at(parent.descriptor, name, label="receipt finalization journal")
    if info is None:
        return None
    owner = parent.identity[3] if isinstance(parent, PrivateDir) else parent.chain[-1][3]
    parent_info = os.fstat(parent.descriptor)
    if (
        int(info.st_uid) != owner
        or int(info.st_gid) != int(parent_info.st_gid)
        or stat.S_IMODE(info.st_mode) != _RECEIPT_JOURNAL_MODE
        or int(info.st_nlink) != 1
    ):
        raise fail("receipt finalization journal ownership or mode is unsafe")
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent.descriptor)
    except OSError as exc:
        raise fail("could not open receipt finalization journal", exc) from exc
    try:
        opened = file_identity(os.fstat(descriptor), label="receipt finalization journal")
        if opened != observed:
            raise fail("receipt finalization journal changed while opening")
        raw = _read_receipt_journal_descriptor(
            descriptor,
            label="receipt finalization journal",
        )
        after = file_identity(os.fstat(descriptor), label="receipt finalization journal")
        named = safe_leaf(
            parent.descriptor,
            name,
            label="receipt finalization journal",
            required_mode=_RECEIPT_JOURNAL_MODE,
        )
        if after != opened or named != opened:
            raise fail("receipt finalization journal changed while reading")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise fail("receipt finalization journal is not canonical JSON", exc) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"core", "final_identity", "identity", "schema", "state"}
            or value.get("schema") != "termdeck.receipt-finalization-journal.v1"
            or value.get("state") != "prepared"
            or value.get("final_identity") is not None
        ):
            raise fail("receipt finalization journal schema is invalid")
        canonical = (
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        if raw != canonical:
            raise fail("receipt finalization journal is not canonical JSON")
        receipt_identity = publication_identity(
            value.get("identity"),
            label="receipt finalization journal receipt identity",
        )
        if receipt_identity[2] != 0o644 or not same_object(receipt_identity, expected_receipt):
            os.close(descriptor)
            return None
        encoded = value.get("core")
        if not isinstance(encoded, str):
            raise fail("receipt finalization journal core is invalid")
        try:
            core = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError) as exc:
            raise fail("receipt finalization journal core is invalid", exc) from exc
        if not core:
            raise fail("receipt finalization journal core is invalid")
        try:
            receipt = json.loads(core.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise fail("receipt finalization journal core is invalid", exc) from exc
        canonical_core = (
            json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        if core != canonical_core:
            raise fail("receipt finalization journal core is not canonical JSON")
        validated = receipt_file_contract(receipt, allow_unsigned=True)
        return {
            "descriptor": descriptor,
            "parent": parent,
            "name": name,
            "identity": opened,
            "receipt_identity": receipt_identity,
            "core": core,
            "receipt": validated,
        }
    except BaseException:
        os.close(descriptor)
        raise


def _find_failed_receipt_finalization_journal(
    context: Context,
    stage: PrivateDir,
    expected_receipt: tuple[int, int, int, int, int, int, int],
) -> dict[str, object] | None:
    candidates: list[tuple[Anchor | PrivateDir, str]] = []
    try:
        names = tuple(os.listdir(context.runtime.descriptor))
    except OSError as exc:
        raise fail("could not enumerate receipt finalization journals", exc) from exc
    for name in names:
        if re.fullmatch(re.escape(_RECEIPT_JOURNAL_PREFIX) + r"[0-9a-f]{32}", name) is not None:
            candidates.append((context.runtime, name))
    if safe_leaf(
        stage.descriptor,
        _RECEIPT_JOURNAL_EVIDENCE,
        label="retained receipt finalization journal",
        required_mode=_RECEIPT_JOURNAL_MODE,
    ) is not None:
        candidates.append((stage, _RECEIPT_JOURNAL_EVIDENCE))
    found: dict[str, object] | None = None
    for parent, name in candidates:
        record = _open_receipt_finalization_journal(parent, name, expected_receipt)
        if record is None:
            continue
        if found is not None:
            os.close(int(record["descriptor"]))
            os.close(int(found["descriptor"]))
            raise fail("multiple receipt finalization journals bind the failed receipt")
        found = record
    return found


def _retain_receipt_finalization_journal(
    context: Context,
    stage: PrivateDir,
    record: dict[str, object],
) -> None:
    parent = record["parent"]
    name = record["name"]
    assert isinstance(parent, (Anchor, PrivateDir))
    assert isinstance(name, str)
    if parent is stage and name == _RECEIPT_JOURNAL_EVIDENCE:
        return
    context.assert_all(phase="before retaining receipt finalization journal")
    stage.assert_current(phase="before retaining receipt finalization journal")
    if safe_leaf(
        stage.descriptor,
        _RECEIPT_JOURNAL_EVIDENCE,
        label="retained receipt finalization journal",
        required_mode=_RECEIPT_JOURNAL_MODE,
    ) is not None:
        raise fail("retained receipt finalization journal already exists")
    try:
        rename_noreplace(
            parent.descriptor,
            name,
            stage.descriptor,
            _RECEIPT_JOURNAL_EVIDENCE,
            label="retain receipt finalization journal",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail("receipt finalization journal changed before retention", exc) from exc
    moved = safe_leaf(
        stage.descriptor,
        _RECEIPT_JOURNAL_EVIDENCE,
        label="retained receipt finalization journal",
        required_mode=_RECEIPT_JOURNAL_MODE,
    )
    if moved is None or not same_after_rename(moved, record["identity"]):
        raise fail("receipt finalization journal changed during retention")
    fsync(parent.descriptor, label="receipt journal parent after retention")
    fsync(stage.descriptor, label="runtime transaction after retaining receipt journal")
    context.assert_all(phase="after retaining receipt finalization journal")
    stage.assert_current(phase="after retaining receipt finalization journal")
    record["parent"] = stage
    record["name"] = _RECEIPT_JOURNAL_EVIDENCE


def _materialize_failed_runtime_core(
    context: Context,
    journal: PrivateDir,
    core: bytes,
) -> tuple[int, int, int, int, int, int, int]:
    """Publish a journal-bound unsigned core into the TLS rollback journal."""
    existing = safe_leaf(
        journal.descriptor,
        _ROLLBACK_PRIOR_RECEIPT,
        label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
        required_mode=0o644,
    )
    if existing is not None:
        raw, observed = read_exact(
            journal.descriptor,
            _ROLLBACK_PRIOR_RECEIPT,
            label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
            required_mode=0o644,
        )
        if raw != core:
            raise fail("TLS journal retains a different failed runtime core")
        return observed
    context.assert_all(phase="before materializing failed runtime core")
    journal.assert_current(phase="before materializing failed runtime core")
    staging = ".rollback-prior-build-receipt.staging-" + secrets.token_hex(16)
    staged = write_exclusive(
        journal.descriptor,
        staging,
        core,
        0o644,
        label="staged failed runtime receipt",
    )
    try:
        written, observed = read_exact(
            journal.descriptor,
            staging,
            label="staged failed runtime receipt",
            required_mode=0o644,
        )
        if written != core or not same_after_rename(observed, staged):
            raise fail("staged failed runtime receipt changed before adoption")
        context.assert_all(phase="immediately before adopting failed runtime core")
        journal.assert_current(phase="immediately before adopting failed runtime core")
        rename_noreplace(
            journal.descriptor,
            staging,
            journal.descriptor,
            _ROLLBACK_PRIOR_RECEIPT,
            label="adopt failed runtime core",
        )
    except BaseException:
        # The no-replace staging leaf is private evidence if a later step is
        # interrupted; leave it for the next descriptor-bound retry.
        raise
    moved = safe_leaf(
        journal.descriptor,
        _ROLLBACK_PRIOR_RECEIPT,
        label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
        required_mode=0o644,
    )
    if moved is None or not same_after_rename(moved, staged):
        raise fail("failed runtime core changed during adoption")
    fsync(journal.descriptor, label="TLS journal after adopting failed runtime core")
    context.assert_all(phase="after adopting failed runtime core")
    journal.assert_current(phase="after adopting failed runtime core")
    return moved


def adopt_failed_runtime_receipt(
    context: Context,
    journal: PrivateDir,
    intent: dict[str, object],
    control_key: bytes,
) -> bool:
    """Bind the failed B receipt to the TLS journal before inverse recovery.

    Runtime publication retains an attestation target in a private
    ``.termdeck-runtime-transaction-*`` directory when finalization fails.
    The TLS inverse must consume that exact inode through descriptors; a
    pathname or a newly copied receipt would allow a replayed B generation to
    steer rollback.  Failed receipts may still be unsigned because the
    attestor refused them, so their TLS and output provenance is checked here
    and the normal inverse validates the A/B leaf topology afterward.
    """
    generation = intent["id"]
    candidate_identity = intent["candidate_identity"]
    old_identity = intent["tls_previous"]
    assert isinstance(generation, str)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(old_identity, tuple)
    existing = safe_leaf(
        journal.descriptor,
        _ROLLBACK_PRIOR_RECEIPT,
        label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
        required_mode=0o644,
    )
    try:
        names = tuple(os.listdir(context.runtime.descriptor))
    except OSError as exc:
        raise fail("could not enumerate failed runtime receipt transactions", exc) from exc
    matched = False
    adopted = False
    for name in names:
        if re.fullmatch(re.escape(_RUNTIME_TRANSACTION_PREFIX) + r"[0-9a-f]{32}", name) is None:
            continue
        stage = open_private_dir(context.runtime, name, label="failed runtime receipt transaction")
        journal_record: dict[str, object] | None = None
        try:
            stage_identity = safe_leaf(
                stage.descriptor,
                "prior",
                label="failed runtime receipt transaction receipt",
                required_mode=0o644,
            )
            if stage_identity is None:
                continue
            try:
                b_receipt, b_identity, b_outputs, b_publication = read_authenticated_runtime_receipt(
                    stage,
                    "prior",
                    control_key,
                    label="failed runtime receipt transaction receipt",
                    allow_unattested=True,
                )
            except TlsError:
                # A receipt interrupted after the preparer created its
                # descriptor-bound journal is intentionally malformed.  The
                # journal's core is the only complete B provenance available;
                # bind it to this exact retained inode before adopting it.
                journal_record = _find_failed_receipt_finalization_journal(
                    context,
                    stage,
                    stage_identity,
                )
                if journal_record is None:
                    continue
                b_receipt = journal_record["receipt"]
                b_identity = stage_identity
                assert isinstance(b_receipt, dict)
                b_outputs = runtime_output_expectations(b_receipt)
                b_publication = tls_publication_contract(b_receipt["tls_publication"])
            current = b_publication["current"]
            prior = b_publication["prior"]
            assert isinstance(current, dict)
            if (
                b_publication["generation"] != generation
                or tuple(current["identity"]) != candidate_identity
                or not isinstance(prior, dict)
                or prior.get("journal") != journal.name
                or tuple(prior.get("journal_identity", ())) != journal.identity
                or prior.get("path") != _PRIOR_TLS
                or tuple(prior.get("identity", ())) != old_identity
            ):
                continue
            if matched:
                raise fail("multiple failed runtime receipts bind the current TLS generation")
            matched = True
            # The output postimages are part of the unsigned B core, but the
            # live leaves must still be exactly those postimages before any
            # inverse mutation.  This also prevents adopting an old private
            # transaction whose receipt happens to name the current journal.
            for path, parent, leaf in (
                ("current-lan-ip", context.runtime, "current-lan-ip"),
                ("nginx/allowed-hosts.map", context.nginx, "allowed-hosts.map"),
            ):
                expected, digest = b_outputs[path]
                if not leaf_matches(parent, leaf, expected, digest, label=path):
                    raise fail(f"failed runtime receipt does not match current {path}")
            if journal_record is not None:
                context.assert_all(phase="before adopting interrupted runtime receipt")
                journal.assert_current(phase="before adopting interrupted runtime receipt")
                stage.assert_current(phase="before adopting interrupted runtime receipt")
                try:
                    held_fd = os.open("prior", _READ_FLAGS, dir_fd=stage.descriptor)
                except OSError as exc:
                    raise fail("could not hold interrupted runtime receipt", exc) from exc
                try:
                    held = file_identity(os.fstat(held_fd), label="held interrupted runtime receipt")
                    if not same_object(held, stage_identity) or held[3] != 1:
                        raise fail("interrupted runtime receipt changed before adoption")
                    existing = _materialize_failed_runtime_core(
                        context,
                        journal,
                        journal_record["core"],
                    )
                    _retain_receipt_finalization_journal(context, stage, journal_record)
                    if not same_object(
                        file_identity(os.fstat(held_fd), label="held interrupted runtime receipt"),
                        held,
                    ):
                        raise fail("interrupted runtime receipt changed during adoption")
                    adopted = True
                finally:
                    os.close(held_fd)
                continue
            if existing is not None:
                if not same_after_rename(existing, b_identity):
                    raise fail("TLS journal already retains a different failed runtime receipt")
                adopted = True
                continue
            context.assert_all(phase="before adopting failed runtime receipt")
            journal.assert_current(phase="before adopting failed runtime receipt")
            stage.assert_current(phase="before adopting failed runtime receipt")
            try:
                held_fd = os.open("prior", _READ_FLAGS, dir_fd=stage.descriptor)
            except OSError as exc:
                raise fail("could not hold failed runtime receipt", exc) from exc
            try:
                held = file_identity(os.fstat(held_fd), label="held failed runtime receipt")
                if not same_object(held, b_identity) or held[3] != 1:
                    raise fail("failed runtime receipt changed before adoption")
                context.assert_all(phase="immediately before adopting failed runtime receipt")
                journal.assert_current(phase="immediately before adopting failed runtime receipt")
                stage.assert_current(phase="immediately before adopting failed runtime receipt")
                try:
                    rename_noreplace(
                        stage.descriptor,
                        "prior",
                        journal.descriptor,
                        _ROLLBACK_PRIOR_RECEIPT,
                        label="adopt failed runtime receipt",
                    )
                except (FileExistsError, FileNotFoundError) as exc:
                    raise fail("failed runtime receipt changed before adoption", exc) from exc
                moved = safe_leaf(
                    journal.descriptor,
                    _ROLLBACK_PRIOR_RECEIPT,
                    label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
                    required_mode=0o644,
                )
                if moved is None or not same_after_rename(moved, held):
                    raise fail("failed runtime receipt changed during adoption")
                fsync(stage.descriptor, label="runtime transaction after failed receipt adoption")
                fsync(journal.descriptor, label="TLS journal after failed receipt adoption")
                fsync(context.runtime.descriptor, label="runtime after failed receipt adoption")
                context.assert_all(phase="after adopting failed runtime receipt")
                journal.assert_current(phase="after adopting failed runtime receipt")
                adopted = True
                existing = moved
            finally:
                os.close(held_fd)
        finally:
            if journal_record is not None:
                os.close(int(journal_record["descriptor"]))
            stage.close()
    return adopted


def quarantine_public_runtime_receipt(
    context: Context,
    journal: PrivateDir,
) -> None:
    """Move a foreign public receipt into the bound TLS journal as evidence."""
    current = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT, required_mode=0o644)
    if current is None:
        return
    name = "rollback-foreign-build-receipt-" + secrets.token_hex(16) + ".json"
    context.assert_all(phase="before quarantining foreign build receipt")
    journal.assert_current(phase="before quarantining foreign build receipt")
    try:
        rename_noreplace(
            context.runtime.descriptor,
            _RECEIPT,
            journal.descriptor,
            name,
            label="quarantine foreign build receipt",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail("build receipt changed before foreign receipt quarantine", exc) from exc
    moved = safe_leaf(journal.descriptor, name, label=f"TLS journal {name}", required_mode=0o644)
    if moved is None or not same_after_rename(moved, current):
        raise fail("foreign build receipt changed during quarantine")
    fsync(journal.descriptor, label="TLS journal after foreign receipt quarantine")
    fsync(context.runtime.descriptor, label="runtime after foreign receipt quarantine")
    context.assert_all(phase="after quarantining foreign build receipt")
    journal.assert_current(phase="after quarantining foreign build receipt")


def restore_unreceipted_prior_leaf(
    context: Context,
    journal: PrivateDir,
    parent: Anchor,
    public_name: str,
    retained_name: str,
    expected: tuple[int, int, int, int, int, int, int],
    digest: str,
    *,
    label: str,
) -> None:
    """Restore only an exact A leaf when no B receipt ever existed."""
    public = safe_leaf(parent.descriptor, public_name, label=label, required_mode=expected[2])
    retained = safe_leaf(journal.descriptor, retained_name, label=f"TLS journal {retained_name}", required_mode=expected[2])
    if public is not None and same_after_rename(public, expected) and retained is None:
        if not leaf_matches(parent, public_name, expected, digest, label=label, allow_renamed=True):
            raise fail(f"{label} changed during unreceipted rollback")
        return
    if public is not None or retained is None or not same_after_rename(retained, expected):
        raise fail(f"{label} has an ambiguous unreceipted rollback topology")
    if not leaf_matches(journal, retained_name, expected, digest, label=f"TLS journal {retained_name}", allow_renamed=True):
        raise fail(f"TLS journal {retained_name} changed")
    context.assert_all(phase=f"immediately before restoring unreceipted {label}")
    journal.assert_current(phase=f"immediately before restoring unreceipted {label}")
    try:
        rename_noreplace(
            journal.descriptor,
            retained_name,
            parent.descriptor,
            public_name,
            label=f"restore unreceipted {label}",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail(f"{label} changed before unreceipted rollback", exc) from exc
    if not leaf_matches(parent, public_name, expected, digest, label=label, allow_renamed=True):
        raise fail(f"{label} changed during unreceipted rollback")
    if safe_leaf(journal.descriptor, retained_name, label=f"TLS journal {retained_name}") is not None:
        raise fail(f"TLS journal {retained_name} remained after unreceipted rollback")
    fsync(journal.descriptor, label=f"TLS journal after restoring unreceipted {label}")
    fsync(parent.descriptor, label=f"parent after restoring unreceipted {label}")
    context.assert_all(phase=f"after restoring unreceipted {label}")
    journal.assert_current(phase=f"after restoring unreceipted {label}")


def exchange_prior_runtime_leaf(
    context: Context,
    journal: PrivateDir,
    parent: Anchor,
    public_name: str,
    retained_name: str,
    current_expected: tuple[int, int, int, int, int, int, int],
    current_digest: str,
    prior_expected: tuple[int, int, int, int, int, int, int],
    prior_digest: str,
    *,
    label: str,
) -> None:
    """Resume one A/B output exchange only in one of two signed topologies."""
    public_is_current = leaf_matches(parent, public_name, current_expected, current_digest, label=label)
    retained_is_prior = leaf_matches(
        journal,
        retained_name,
        prior_expected,
        prior_digest,
        label=f"TLS journal {retained_name}",
        allow_renamed=True,
    )
    if public_is_current and retained_is_prior:
        context.assert_all(phase=f"immediately before restoring {label}")
        journal.assert_current(phase=f"immediately before restoring {label}")
        try:
            rename_exchange_between(
                parent.descriptor,
                public_name,
                journal.descriptor,
                retained_name,
                label=f"restore prior {label}",
            )
        except (FileNotFoundError, FileExistsError) as exc:
            raise fail(f"{label} changed before rollback exchange", exc) from exc
        if not (
            leaf_matches(parent, public_name, prior_expected, prior_digest, label=label, allow_renamed=True)
            and leaf_matches(
                journal,
                retained_name,
                current_expected,
                current_digest,
                label=f"TLS journal {retained_name}",
                allow_renamed=True,
            )
        ):
            raise fail(f"{label} changed during rollback exchange")
        fsync(parent.descriptor, label=f"parent after restoring {label}")
        fsync(journal.descriptor, label=f"TLS journal after restoring {label}")
        context.assert_all(phase=f"after restoring {label}")
        journal.assert_current(phase=f"after restoring {label}")
        return
    public_is_prior = leaf_matches(parent, public_name, prior_expected, prior_digest, label=label, allow_renamed=True)
    retained_is_current = leaf_matches(
        journal,
        retained_name,
        current_expected,
        current_digest,
        label=f"TLS journal {retained_name}",
        allow_renamed=True,
    )
    if public_is_prior and retained_is_current:
        return
    raise fail(f"{label} has an ambiguous rollback exchange topology")


def validate_receipt_against_current_runtime(
    receipt: dict[str, object],
    receipt_identity: tuple[int, int, int, int, int, int, int],
    context: Context,
    control_key: bytes,
    *,
    expected_tls_identity: tuple[int, int, int, int],
) -> None:
    """Validate a strict receipt whether it is public or still journal-held."""
    source_postimages = capture_receipt_source_postimages(context)
    if {path: digest for path, (_identity, digest) in source_postimages.items()} != receipt["files"]:
        raise fail("restored build receipt does not match current runtime source files")
    if "producer_commitment" in receipt:
        verify_producer_commitment(receipt, receipt_identity, source_postimages, control_key)
    verify_runtime_publication(receipt, context)
    publication = tls_publication_contract(receipt["tls_publication"])
    current = publication["current"]
    assert isinstance(current, dict)
    if tuple(current["identity"]) != expected_tls_identity or context.tls.chain[-1] != expected_tls_identity:
        raise fail("restored build receipt does not bind the restored TLS generation")
    lan_ip = receipt["lan_ip"]
    assert isinstance(lan_ip, str)
    manifest = current["manifest"]
    assert isinstance(manifest, dict)
    verify_candidate_manifest(context.tls, manifest, lan_ip)
    context.assert_all(phase="after validating restored runtime receipt")


def validate_receipt_before_source_restored_inverse(
    receipt: dict[str, object],
    receipt_identity: tuple[int, int, int, int, int, int, int],
    prior_receipt: dict[str, object],
    context: Context,
    control_key: bytes,
    *,
    expected_tls_identity: tuple[int, int, int, int],
) -> None:
    """Allow only the authenticated A executable/config before inverse TLS.

    Activation restores these two source leaves from its retained A copies,
    so their inodes differ from both receipts while B TLS and generated
    outputs are still public.  Their bytes and modes must exactly match the
    authenticated A receipt; all TLS sources keep B's strict full identities.
    """
    source_postimages = capture_receipt_source_postimages(context)
    restored_sources = {"bin/termdeck-web-server", "nginx/nginx.conf"}
    required_modes = {
        "bin/termdeck-web-server": 0o755,
        "nginx/nginx.conf": 0o644,
    }
    for path, (identity, digest) in source_postimages.items():
        if path in restored_sources:
            if digest != prior_receipt["files"][path] or identity[2] != required_modes[path]:
                raise fail(f"TLS rollback source does not match authenticated A bytes and mode: {path}")
        elif digest != receipt["files"][path]:
            raise fail(f"TLS rollback source does not match the current B generation: {path}")
    verify_runtime_publication(receipt, context)
    publication = tls_publication_contract(receipt["tls_publication"])
    current = publication["current"]
    assert isinstance(current, dict)
    if tuple(current["identity"]) != expected_tls_identity or context.tls.chain[-1] != expected_tls_identity:
        raise fail("public B receipt does not bind the forward TLS generation")
    lan_ip = receipt["lan_ip"]
    manifest = current["manifest"]
    assert isinstance(lan_ip, str)
    assert isinstance(manifest, dict)
    verify_candidate_manifest(context.tls, manifest, lan_ip)
    context.assert_all(phase="after validating source-restored TLS inverse")


def reissued_receipt_core_bytes(
    base: dict[str, object],
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    runtime_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    tls_publication: dict[str, object],
) -> bytes:
    lan_ip = base["lan_ip"]
    https_port = base["https_port"]
    assert isinstance(lan_ip, str)
    assert isinstance(https_port, int)
    files = {path: digest for path, (_identity, digest) in source_postimages.items()}
    outputs: dict[str, dict[str, object]] = {}
    for path, (identity, digest) in runtime_outputs.items():
        outputs[path] = {
            "identity": identity_json(identity),
            "mode": identity[2],
            "path": path,
            "sha256": digest,
        }
    receipt: dict[str, object] = {
        "files": files,
        "https_port": https_port,
        "lan_ip": lan_ip,
        "ok": True,
        "runtime_publication": {
            "generation": secrets.token_hex(16),
            "outputs": outputs,
        },
        "schema": _RUNTIME_RECEIPT_SCHEMA,
        "tls_publication": tls_publication,
    }
    # Reject an accidentally widened schema before retaining or publishing the
    # unsigned core.  The root signer will add producer_attestation only after
    # this core occupies the canonical public path.
    receipt_file_contract(receipt, allow_unsigned=True)
    runtime_output_expectations(receipt)
    tls_publication_contract(tls_publication)
    return (json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def rewrite_private_regular(
    parent: PrivateDir,
    name: str,
    expected: tuple[int, int, int, int, int, int, int],
    data: bytes,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int, int]:
    """Fill an already-created private receipt after its inode is known."""
    try:
        descriptor = os.open(name, os.O_WRONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent.descriptor)
    except OSError as exc:
        raise fail(f"could not reopen {label}", exc) from exc
    try:
        opened = file_identity(os.fstat(descriptor), label=label)
        if not same_object(opened, expected):
            raise fail(f"{label} changed before writing its final payload")
        os.ftruncate(descriptor, 0)
        position = 0
        while position < len(data):
            written = os.write(descriptor, data[position:])
            if written <= 0:
                raise fail(f"could not write {label}")
            position += written
        fsync(descriptor, label=label)
        observed = file_identity(os.fstat(descriptor), label=label)
        named = safe_leaf(parent.descriptor, name, label=label, required_mode=0o644)
        if observed[3] != 1 or named is None or not same_object(named, observed):
            raise fail(f"{label} changed while writing its final payload")
        fsync(parent.descriptor, label=f"parent after writing {label}")
        return observed
    finally:
        os.close(descriptor)


def restored_runtime_output_snapshot(
    context: Context,
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str]]:
    values: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] = {}
    for path, parent, name in (
        ("current-lan-ip", context.runtime, "current-lan-ip"),
        ("nginx/allowed-hosts.map", context.nginx, "allowed-hosts.map"),
    ):
        data, identity = read_exact(parent.descriptor, name, label=path, required_mode=0o600)
        values[path] = (identity, hashlib.sha256(data).hexdigest())
    return values


def _restore_receipt_staging_names(journal: PrivateDir) -> list[str]:
    """List only descriptor-bound restored-receipt staging evidence."""

    names: list[str] = []
    try:
        entries = os.listdir(journal.descriptor)
    except OSError as exc:
        raise fail("could not inspect TLS journal restored receipt staging", exc) from exc
    for name in sorted(entries):
        if not name.startswith(_ROLLBACK_RESTORED_RECEIPT_STAGING_PREFIX):
            continue
        component(name, label="restored receipt staging name")
        if safe_leaf(
            journal.descriptor,
            name,
            label=f"TLS journal {name}",
            required_mode=0o644,
        ) is not None:
            names.append(name)
    return names


def _stage_restored_runtime_receipt(
    journal: PrivateDir,
    payload: bytes,
) -> tuple[int, int, int, int, int, int, int]:
    """Durably construct and promote a restored receipt through two names.

    The initial newline is intentional: it creates a durable, recognizable
    provisional inode before the full core is written.  A crash or injected
    write failure therefore leaves its exact partial staging inode as private
    evidence while a later process allocates a fresh staging inode and never
    mutates the failed bytes.
    """

    for _attempt in range(32):
        name = _ROLLBACK_RESTORED_RECEIPT_STAGING_PREFIX + secrets.token_hex(16)
        try:
            descriptor = os.open(name, _WRITE_FLAGS, 0o644, dir_fd=journal.descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise fail("could not create restored runtime receipt staging", exc) from exc
        try:
            os.fchmod(descriptor, 0o644)
            if os.write(descriptor, b"\n") != 1:
                raise fail("could not create restored runtime receipt provisional payload")
            fsync(descriptor, label="restored runtime receipt provisional payload")
            fsync(journal.descriptor, label="TLS journal after restored receipt provisional payload")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise fail("could not write restored runtime receipt staging")
                offset += written
            fsync(descriptor, label="restored runtime receipt staging")
            observed = file_identity(os.fstat(descriptor), label="restored runtime receipt staging")
            named = safe_leaf(
                journal.descriptor,
                name,
                label=f"TLS journal {name}",
                required_mode=0o644,
            )
            if (
                named is None
                or not same_object(named, observed)
                or observed[2] != 0o644
                or observed[3] != 1
                or observed[4] != len(payload)
            ):
                raise fail("restored runtime receipt staging changed while writing")
            fsync(journal.descriptor, label="TLS journal after restored runtime receipt staging")
            journal.assert_current(phase="before promoting restored runtime receipt staging")
            rename_noreplace(
                journal.descriptor,
                name,
                journal.descriptor,
                _ROLLBACK_RESTORED_RECEIPT,
                label="promote restored runtime receipt staging",
            )
            promoted = safe_leaf(
                journal.descriptor,
                _ROLLBACK_RESTORED_RECEIPT,
                label=f"TLS journal {_ROLLBACK_RESTORED_RECEIPT}",
                required_mode=0o644,
            )
            if promoted is None or not same_after_rename(promoted, observed):
                raise fail("restored runtime receipt staging changed during promotion")
            fsync(journal.descriptor, label="TLS journal after promoting restored runtime receipt")
            return promoted
        finally:
            os.close(descriptor)
    raise fail("could not allocate restored runtime receipt staging")


def _promote_complete_restored_runtime_receipt_staging(
    journal: PrivateDir,
) -> tuple[int, int, int, int, int, int, int] | None:
    """Promote one complete private staging core after a crash before rename."""

    for name in _restore_receipt_staging_names(journal):
        raw, identity = read_exact(
            journal.descriptor,
            name,
            label=f"TLS journal {name}",
            required_mode=0o644,
        )
        try:
            receipt_file_contract(json.loads(raw.decode("utf-8")), allow_unsigned=True)
        except (TlsError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        journal.assert_current(phase="before promoting recovered restored receipt staging")
        try:
            rename_noreplace(
                journal.descriptor,
                name,
                journal.descriptor,
                _ROLLBACK_RESTORED_RECEIPT,
                label="promote recovered restored receipt staging",
            )
        except FileExistsError:
            return safe_leaf(
                journal.descriptor,
                _ROLLBACK_RESTORED_RECEIPT,
                label=f"TLS journal {_ROLLBACK_RESTORED_RECEIPT}",
                required_mode=0o644,
            )
        promoted = safe_leaf(
            journal.descriptor,
            _ROLLBACK_RESTORED_RECEIPT,
            label=f"TLS journal {_ROLLBACK_RESTORED_RECEIPT}",
            required_mode=0o644,
        )
        if promoted is None or not same_after_rename(promoted, identity):
            raise fail("recovered restored runtime receipt staging changed during promotion")
        fsync(journal.descriptor, label="TLS journal after promoting recovered restored receipt")
        return promoted
    return None


def restore_reissued_runtime_receipt(
    base_receipt: dict[str, object],
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
    *,
    expected_tls_identity: tuple[int, int, int, int],
) -> tuple[int, int, int, int, int, int, int]:
    """Publish a fresh A receipt after moves changed A output ctimes.

    A rename preserves an output inode but changes its ctime, which strict v2
    intentionally commits.  Reusing A's old receipt would therefore be false
    proof.  The old receipt stays in the journal as evidence while this
    descriptor-rooted final publication signs the restored postimage last.
    """
    current = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT, required_mode=0o644)
    staged = safe_leaf(
        journal.descriptor,
        _ROLLBACK_RESTORED_RECEIPT,
        label=f"TLS journal {_ROLLBACK_RESTORED_RECEIPT}",
        required_mode=0o644,
    )
    if staged is None:
        staged = _promote_complete_restored_runtime_receipt_staging(journal)
    if current is not None:
        try:
            receipt, identity, _outputs, _publication = read_authenticated_runtime_receipt(
                context.runtime,
                _RECEIPT,
                control_key,
                label=_RECEIPT,
            )
        except TlsError:
            if staged is None:
                raise
        else:
            validate_receipt_against_current_runtime(
                receipt,
                identity,
                context,
                control_key,
                expected_tls_identity=expected_tls_identity,
            )
            return identity
    if staged is None:
        source_postimages = capture_receipt_source_postimages(context)
        if {path: digest for path, (_identity, digest) in source_postimages.items()} != base_receipt["files"]:
            raise fail("restored runtime source files no longer match the retained A receipt")
        tls_publication = current_tls_publication(context, control_key)
        current_tls = tls_publication["current"]
        assert isinstance(current_tls, dict)
        if tuple(current_tls["identity"]) != expected_tls_identity:
            raise fail("restored TLS provenance does not own the A generation")
        outputs = restored_runtime_output_snapshot(context)
        context.assert_all(phase="immediately before retaining restored runtime core")
        journal.assert_current(phase="immediately before retaining restored runtime core")
        payload = reissued_receipt_core_bytes(
            base_receipt,
            source_postimages,
            outputs,
            tls_publication,
        )
        staged = _stage_restored_runtime_receipt(journal, payload)
    core_raw, staged_identity = read_exact(
        journal.descriptor,
        _ROLLBACK_RESTORED_RECEIPT,
        label=f"TLS journal {_ROLLBACK_RESTORED_RECEIPT}",
        required_mode=0o644,
    )
    if staged_identity != staged:
        raise fail("restored runtime core changed while retained")
    try:
        core = receipt_file_contract(json.loads(core_raw.decode("utf-8")), allow_unsigned=True)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("retained restored runtime core is invalid", exc) from exc
    if "producer_attestation" in core or "producer_commitment" in core:
        raise fail("retained restored runtime core is already authenticated")
    validate_receipt_against_current_runtime(
        core,
        staged_identity,
        context,
        control_key,
        expected_tls_identity=expected_tls_identity,
    )

    if current is not None:
        current_raw, observed_current = read_exact(
            context.runtime.descriptor,
            _RECEIPT,
            label=_RECEIPT,
            required_mode=0o644,
        )
        if observed_current != current:
            raise fail("restored runtime receipt changed while resuming finalization")
        if current_raw != core_raw:
            # A killed in-place finalizer can leave a partial postimage.  Move
            # the exact failed inode into the authenticated transaction
            # directory as evidence before publishing a fresh canonical core.
            failed_name = "rollback-failed-build-receipt-" + secrets.token_hex(16) + ".json"
            try:
                rename_noreplace(
                    context.runtime.descriptor,
                    _RECEIPT,
                    journal.descriptor,
                    failed_name,
                    label="retain interrupted restored runtime receipt",
                )
            except (FileExistsError, FileNotFoundError) as exc:
                raise fail("restored runtime receipt changed before recovery", exc) from exc
            retained_failed = safe_leaf(
                journal.descriptor,
                failed_name,
                label=f"TLS journal {failed_name}",
                required_mode=0o644,
            )
            if retained_failed is None or not same_after_rename(retained_failed, current):
                raise fail("interrupted restored runtime receipt changed while retaining evidence")
            fsync(journal.descriptor, label="TLS journal after retaining interrupted restored receipt")
            fsync(context.runtime.descriptor, label="runtime after retaining interrupted restored receipt")
            current = None

    if current is None:
        context.assert_all(phase="immediately before publishing restored runtime core")
        journal.assert_current(phase="immediately before publishing restored runtime core")
        current = write_exclusive(
            context.runtime.descriptor,
            _RECEIPT,
            core_raw,
            0o644,
            label=_RECEIPT,
        )
    publication = core["runtime_publication"]
    assert isinstance(publication, dict)
    generation = publication["generation"]
    assert isinstance(generation, str)
    run_attestation_helper(
        context.runtime.path + "/" + _RECEIPT,
        transaction_id=generation,
        label=_RECEIPT,
    )
    receipt, public_identity, _outputs, _publication = read_authenticated_runtime_receipt(
        context.runtime,
        _RECEIPT,
        control_key,
        label=_RECEIPT,
    )
    validate_receipt_against_current_runtime(
        receipt,
        public_identity,
        context,
        control_key,
        expected_tls_identity=expected_tls_identity,
    )
    context.assert_all(phase="after publishing restored runtime receipt")
    journal.assert_current(phase="after publishing restored runtime receipt")
    if public_identity[:4] != current[:4]:
        raise fail("attestation did not finalize the published restored runtime core inode")
    return public_identity


def move_current_receipt_for_rollback(
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
    *,
    prior_receipt: dict[str, object] | None = None,
) -> None:
    """Tombstone only the currently authenticated B receipt before inverse TLS."""
    existing = safe_leaf(journal.descriptor, _ROLLBACK_PRIOR_RECEIPT, label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}")
    current = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    if existing is not None:
        if current is not None:
            raise fail("build receipt changed during TLS rollback")
        return
    if current is None:
        return
    if prior_receipt is None:
        expected = managed_receipt(context, control_key)
    else:
        receipt, expected, _outputs, _publication = read_authenticated_runtime_receipt(
            context.runtime, _RECEIPT, control_key, label=_RECEIPT,
        )
        validate_receipt_before_source_restored_inverse(
            receipt, expected, prior_receipt, context, control_key,
            expected_tls_identity=context.tls.chain[-1],
        )
    if expected is None or expected != current:
        raise fail("build receipt changed during TLS rollback")
    context.assert_all(phase="immediately before invalidating build receipt for TLS rollback")
    journal.assert_current(phase="immediately before invalidating build receipt for TLS rollback")
    try:
        rename_noreplace(
            context.runtime.descriptor,
            _RECEIPT,
            journal.descriptor,
            _ROLLBACK_PRIOR_RECEIPT,
            label="invalidate build receipt for TLS rollback",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise fail("build receipt changed before TLS rollback", exc) from exc
    moved = safe_leaf(journal.descriptor, _ROLLBACK_PRIOR_RECEIPT, label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}")
    if (
        moved is None
        or not same_after_rename(moved, expected)
        or safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None
    ):
        if moved is not None:
            try:
                restored = restore_held_foreign_regular(
                    journal,
                    _ROLLBACK_PRIOR_RECEIPT,
                    context.runtime,
                    _RECEIPT,
                    context=context,
                    label="foreign build receipt moved during TLS rollback invalidation",
                )
            except BaseException as exc:
                raise fail("build receipt changed during TLS rollback and could not be safely restored", exc) from exc
            if restored:
                raise fail("build receipt changed during TLS rollback; restored held foreign receipt")
        raise fail("build receipt changed during TLS rollback; retained ambiguous receipt")
    fsync(journal.descriptor, label="TLS journal after rollback receipt invalidation")
    fsync(context.runtime.descriptor, label="runtime after rollback receipt invalidation")


def restored_runtime_result(
    generation: str,
    receipt_identity: tuple[int, int, int, int, int, int, int],
    context: Context,
    journal: PrivateDir,
    old_identity: tuple[int, int, int, int],
    previous_manifest: dict[str, dict[str, object]],
    candidate_identity: tuple[int, int, int, int],
    candidate_manifest_value: dict[str, dict[str, object]],
    *,
    retained_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] | None,
) -> str:
    receipt_raw, observed_receipt = read_exact(context.runtime.descriptor, _RECEIPT, label=_RECEIPT, required_mode=0o644)
    if observed_receipt != receipt_identity:
        raise fail("restored runtime receipt changed before reporting its result")
    try:
        receipt = receipt_file_contract(json.loads(receipt_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("restored runtime receipt became invalid", exc) from exc
    publication = receipt["runtime_publication"]
    assert isinstance(publication, dict)
    runtime_generation = publication["generation"]
    assert isinstance(runtime_generation, str)
    restored_outputs = restored_runtime_output_snapshot(context)
    retained: dict[str, object] = {
        "identity": identity_json(candidate_identity),
        "journal": journal.name,
        "journal_identity": identity_json(journal.identity),
        "manifest": candidate_manifest_value,
        "path": _PRIOR_TLS,
    }
    if retained_outputs is not None:
        retained["outputs"] = {
            path: {"identity": identity_json(identity), "sha256": digest}
            for path, (identity, digest) in sorted(retained_outputs.items())
        }
    return json.dumps(
        {
            "action": "restored-runtime-generation",
            "generation": generation,
            "ok": True,
            "receipt": {
                "identity": identity_json(receipt_identity),
                "path": _RECEIPT,
                "sha256": hashlib.sha256(receipt_raw).hexdigest(),
            },
            "restored": {
                "outputs": {
                    path: {"identity": identity_json(identity), "sha256": digest}
                    for path, (identity, digest) in sorted(restored_outputs.items())
                },
                "runtime_generation": runtime_generation,
                "tls": {
                    "identity": identity_json(old_identity),
                    "manifest": previous_manifest,
                    "path": "tls",
                },
            },
            "retained": retained,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def verify_rollback_tls_preimages(
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
) -> None:
    """Reject altered retained TLS before invalidating a valid public receipt."""
    old_identity = intent["tls_previous"]
    candidate_identity = intent["candidate_identity"]
    previous_manifest = intent["tls_previous_manifest"]
    candidate_manifest_value = intent["candidate_manifest"]
    lan_ip = intent["lan_ip"]
    previous_lan_ip = intent.get("tls_previous_lan_ip", lan_ip)
    assert isinstance(old_identity, tuple)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(previous_manifest, dict)
    assert isinstance(candidate_manifest_value, dict)
    assert isinstance(lan_ip, str)
    assert isinstance(previous_lan_ip, str)
    current = context.tls.chain[-1]
    retained = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
    if current == candidate_identity and retained == old_identity:
        current_manifest, current_ip = candidate_manifest_value, lan_ip
        retained_manifest, retained_ip = previous_manifest, previous_lan_ip
    elif current == old_identity and retained == candidate_identity:
        current_manifest, current_ip = previous_manifest, previous_lan_ip
        retained_manifest, retained_ip = candidate_manifest_value, lan_ip
    else:
        raise fail("TLS rollback no longer owns its exact forward generations")
    context.assert_all(phase="before validating TLS rollback preimages")
    journal.assert_current(phase="before validating TLS rollback preimages")
    verify_candidate_manifest(context.tls, current_manifest, current_ip)
    prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=retained)
    try:
        verify_candidate_manifest(prior, retained_manifest, retained_ip)
    finally:
        prior.close()
    context.assert_all(phase="after validating TLS rollback preimages")
    journal.assert_current(phase="after validating TLS rollback preimages")


def verify_rollback_runtime_preimages(
    context: Context,
    journal: PrivateDir,
    a_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    b_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] | None,
) -> None:
    """Prove every A/B leaf topology before the first rollback mutation."""
    context.assert_all(phase="before validating runtime rollback preimages")
    journal.assert_current(phase="before validating runtime rollback preimages")
    for path, parent, public_name, retained_name in (
        ("current-lan-ip", context.runtime, "current-lan-ip", _PRIOR_CURRENT),
        ("nginx/allowed-hosts.map", context.nginx, "allowed-hosts.map", _PRIOR_HOSTS),
    ):
        a_identity, a_digest = a_outputs[path]
        public_is_a = leaf_matches(parent, public_name, a_identity, a_digest, label=path, allow_renamed=True)
        retained_is_a = leaf_matches(
            journal, retained_name, a_identity, a_digest, label=f"TLS journal {retained_name}", allow_renamed=True,
        )
        if b_outputs is None:
            if (
                public_is_a
                and safe_leaf(journal.descriptor, retained_name, label=f"TLS journal {retained_name}") is None
            ) or (
                safe_leaf(parent.descriptor, public_name, label=path) is None
                and retained_is_a
            ):
                continue
        else:
            b_identity, b_digest = b_outputs[path]
            public_is_b = leaf_matches(parent, public_name, b_identity, b_digest, label=path)
            retained_is_b = leaf_matches(
                journal, retained_name, b_identity, b_digest, label=f"TLS journal {retained_name}", allow_renamed=True,
            )
            if (public_is_b and retained_is_a) or (public_is_a and retained_is_b):
                continue
        raise fail(f"{path} has an ambiguous rollback preimage topology")
    context.assert_all(phase="after validating runtime rollback preimages")
    journal.assert_current(phase="after validating runtime rollback preimages")


def restore_v2_runtime_generation(
    generation: str,
    intent: dict[str, object],
    context: Context,
    journal: PrivateDir,
    control_key: bytes,
) -> str:
    """Durably restore all of A, then sign its moved output postimages anew.

    A v2 receipt commits output ctime as well as inode.  Moving A into B's
    journal preserves its data and inode but deliberately changes ctime, so a
    byte-copy or a moved old receipt cannot be valid proof.  This inverse keeps
    the old receipt and B generation in the authenticated journal, restores A
    with descriptor exchanges, then emits one fresh receipt as the final
    mutation.  Every restart state is identified by signed A/B leaves rather
    than a best-effort phase marker.
    """
    previous_manifest = intent.get("tls_previous_manifest")
    candidate_manifest_value = intent["candidate_manifest"]
    candidate_identity = intent["candidate_identity"]
    old_identity = intent["tls_previous"]
    lan_ip = intent["lan_ip"]
    previous_lan_ip = intent.get("tls_previous_lan_ip")
    assert isinstance(previous_manifest, dict)
    assert isinstance(candidate_manifest_value, dict)
    assert isinstance(candidate_identity, tuple)
    assert isinstance(old_identity, tuple)
    assert isinstance(lan_ip, str)
    if not isinstance(previous_lan_ip, str):
        raise fail("TLS rollback has no authenticated prior LAN address")
    if not marker_present(journal, _COMPLETE, b"complete\n", control_key):
        raise fail("TLS restore requires an authenticated completed forward generation")

    a_receipt, _a_receipt_identity, a_outputs, a_publication = read_authenticated_runtime_receipt(
        journal,
        _PRIOR_RECEIPT,
        control_key,
        label=f"TLS journal {_PRIOR_RECEIPT}",
    )
    expected_a_receipt = intent["receipt_previous"]
    if not isinstance(expected_a_receipt, tuple) or not same_after_rename(_a_receipt_identity, expected_a_receipt):
        raise fail("retained A build receipt does not match its authenticated TLS intent")
    a_current = a_publication["current"]
    assert isinstance(a_current, dict)
    if tuple(a_current["identity"]) != old_identity:
        raise fail("retained A build receipt does not bind the retained TLS generation")

    prior_tls = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
    current_tls = context.tls.chain[-1]
    public_receipt = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT, required_mode=0o644)
    rollback_receipt = safe_leaf(
        journal.descriptor,
        _ROLLBACK_PRIOR_RECEIPT,
        label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
        required_mode=0o644,
    )

    # A prior invocation may already have published the reissued receipt but
    # died before its terminal marker.  Its strict contents are the durable
    # proof; no byte restoration is attempted.
    if current_tls == old_identity and prior_tls == candidate_identity and public_receipt is not None:
        current_receipt, current_identity, _current_outputs, current_publication = read_authenticated_runtime_receipt(
            context.runtime,
            _RECEIPT,
            control_key,
            label=_RECEIPT,
        )
        current_tls_publication = current_publication["current"]
        assert isinstance(current_tls_publication, dict)
        if tuple(current_tls_publication["identity"]) != old_identity:
            raise fail("public receipt has an ambiguous rollback generation")
        verify_candidate_manifest(context.tls, previous_manifest, previous_lan_ip)
        verify_inverse_rotation_evidence(intent, context, journal)
        validate_receipt_against_current_runtime(
            current_receipt,
            current_identity,
            context,
            control_key,
            expected_tls_identity=old_identity,
        )
        retained_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] | None = None
        if rollback_receipt is not None:
            b_receipt, _b_identity, retained_outputs, b_publication = read_authenticated_runtime_receipt(
                journal,
                _ROLLBACK_PRIOR_RECEIPT,
                control_key,
                label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
                allow_unattested=True,
            )
            b_current = b_publication["current"]
            assert isinstance(b_current, dict)
            if tuple(b_current["identity"]) != candidate_identity:
                raise fail("retained B receipt does not bind the retained TLS generation")
        verify_rollback_runtime_preimages(context, journal, a_outputs, retained_outputs)
        marker(journal, _ROLLBACK_OUTPUTS_EXCHANGED, b"rollback-outputs-exchanged\n", context=context, control_key=control_key)
        marker(journal, _ROLLBACK_RECEIPT_RESTORED, b"rollback-receipt-restored\n", context=context, control_key=control_key)
        marker(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", context=context, control_key=control_key)
        return restored_runtime_result(
            generation,
            current_identity,
            context,
            journal,
            old_identity,
            previous_manifest,
            candidate_identity,
            candidate_manifest_value,
            retained_outputs=retained_outputs,
        )

    b_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] | None = None
    if public_receipt is not None:
        if rollback_receipt is not None:
            raise fail("TLS rollback has both public and retained B receipts")
        b_receipt, b_receipt_identity, b_outputs, b_publication = read_authenticated_runtime_receipt(
            context.runtime,
            _RECEIPT,
            control_key,
            label=_RECEIPT,
        )
        b_current = b_publication["current"]
        assert isinstance(b_current, dict)
        if tuple(b_current["identity"]) != candidate_identity or current_tls != candidate_identity:
            raise fail("public B receipt does not bind the forward TLS generation")
        validate_receipt_before_source_restored_inverse(
            b_receipt,
            b_receipt_identity,
            a_receipt,
            context,
            control_key,
            expected_tls_identity=candidate_identity,
        )
    elif rollback_receipt is not None:
        _b_receipt, _b_receipt_identity, b_outputs, b_publication = read_authenticated_runtime_receipt(
            journal,
            _ROLLBACK_PRIOR_RECEIPT,
            control_key,
            label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
            allow_unattested=True,
        )
        b_current = b_publication["current"]
        assert isinstance(b_current, dict)
        if tuple(b_current["identity"]) != candidate_identity:
            raise fail("retained B receipt does not bind the forward TLS generation")
    # An authenticated receipt describes expected bytes, not the current
    # contents of retained leaves.  Check all of A before moving B's receipt
    # or exchanging any TLS/output name, including an unreceipted rollback.
    verify_rollback_runtime_preimages(context, journal, a_outputs, b_outputs)
    marker(journal, _ROLLBACK_READY, b"rollback-ready\n", context=context, control_key=control_key)
    if public_receipt is not None:
        move_current_receipt_for_rollback(context, journal, control_key, prior_receipt=a_receipt)
        _b_receipt, observed_b_receipt, b_outputs, b_publication = read_authenticated_runtime_receipt(
            journal,
            _ROLLBACK_PRIOR_RECEIPT,
            control_key,
            label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
            allow_unattested=True,
        )
        if not same_after_rename(observed_b_receipt, b_receipt_identity):
            raise fail("retained B receipt changed during rollback invalidation")

    if current_tls == candidate_identity and prior_tls == old_identity:
        verify_relocated_rotation_evidence(intent, context, journal)
        context.assert_all(phase="immediately before full runtime rollback TLS exchange")
        journal.assert_current(phase="immediately before full runtime rollback TLS exchange")
        rename_exchange_between(
            context.runtime.descriptor,
            "tls",
            journal.descriptor,
            _PRIOR_TLS,
            label="full runtime rollback TLS exchange",
        )
        fsync(context.runtime.descriptor, label="runtime after full runtime rollback TLS exchange")
        fsync(journal.descriptor, label="TLS journal after full runtime rollback TLS exchange")
        if (
            child_identity(context.runtime, "tls", label="TLS public entry") != old_identity
            or child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}") != candidate_identity
        ):
            raise fail("TLS directory changed during full runtime rollback exchange")
        context.rebind_tls(old_identity)
    elif current_tls == old_identity and prior_tls == candidate_identity:
        context.rebind_tls(old_identity)
    else:
        raise fail("TLS rollback no longer owns its exact forward generations")
    verify_candidate_manifest(context.tls, previous_manifest, previous_lan_ip)
    verify_inverse_rotation_evidence(intent, context, journal)
    marker(journal, _ROLLBACK_EXCHANGED, b"rollback-exchanged\n", context=context, control_key=control_key)

    current_expected, current_digest = a_outputs["current-lan-ip"]
    hosts_expected, hosts_digest = a_outputs["nginx/allowed-hosts.map"]
    retained_outputs: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] | None = None
    if b_outputs is None:
        restore_unreceipted_prior_leaf(
            context,
            journal,
            context.runtime,
            "current-lan-ip",
            _PRIOR_CURRENT,
            current_expected,
            current_digest,
            label="current-lan-ip",
        )
        restore_unreceipted_prior_leaf(
            context,
            journal,
            context.nginx,
            "allowed-hosts.map",
            _PRIOR_HOSTS,
            hosts_expected,
            hosts_digest,
            label="nginx/allowed-hosts.map",
        )
    else:
        b_current_expected, b_current_digest = b_outputs["current-lan-ip"]
        b_hosts_expected, b_hosts_digest = b_outputs["nginx/allowed-hosts.map"]
        exchange_prior_runtime_leaf(
            context,
            journal,
            context.runtime,
            "current-lan-ip",
            _PRIOR_CURRENT,
            b_current_expected,
            b_current_digest,
            current_expected,
            current_digest,
            label="current-lan-ip",
        )
        exchange_prior_runtime_leaf(
            context,
            journal,
            context.nginx,
            "allowed-hosts.map",
            _PRIOR_HOSTS,
            b_hosts_expected,
            b_hosts_digest,
            hosts_expected,
            hosts_digest,
            label="nginx/allowed-hosts.map",
        )
        retained_outputs = {
            "current-lan-ip": (b_current_expected, b_current_digest),
            "nginx/allowed-hosts.map": (b_hosts_expected, b_hosts_digest),
        }
    marker(journal, _ROLLBACK_OUTPUTS_EXCHANGED, b"rollback-outputs-exchanged\n", context=context, control_key=control_key)
    receipt_identity = restore_reissued_runtime_receipt(
        a_receipt,
        context,
        journal,
        control_key,
        expected_tls_identity=old_identity,
    )
    marker(journal, _ROLLBACK_RECEIPT_RESTORED, b"rollback-receipt-restored\n", context=context, control_key=control_key)
    marker(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", context=context, control_key=control_key)
    return restored_runtime_result(
        generation,
        receipt_identity,
        context,
        journal,
        old_identity,
        previous_manifest,
        candidate_identity,
        candidate_manifest_value,
        retained_outputs=retained_outputs,
    )


def restore_tls_generation(
    generation: str,
    context: Context,
    control_key: bytes,
) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise fail("invalid TLS generation restore request")
    name = _JOURNAL_PREFIX + generation
    journal = open_private_dir(context.runtime, name, label="TLS exchange journal")
    try:
        intent = parse_intent(journal, control_key)
        if intent["id"] != generation:
            raise fail("TLS restore request does not bind its journal")
        previous_manifest = intent.get("tls_previous_manifest")
        if previous_manifest is None:
            return json.dumps(
                {
                    "action": "no-prior-tls-generation",
                    "generation": generation,
                    "ok": False,
                    "prior": None,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        assert isinstance(previous_manifest, dict)
        candidate_manifest_value = intent["candidate_manifest"]
        candidate_identity = intent["candidate_identity"]
        old_identity = intent["tls_previous"]
        lan_ip = intent["lan_ip"]
        assert isinstance(candidate_manifest_value, dict)
        assert isinstance(candidate_identity, tuple)
        assert isinstance(old_identity, tuple)
        assert isinstance(lan_ip, str)
        previous_lan_ip = intent.get("tls_previous_lan_ip", lan_ip)
        assert isinstance(previous_lan_ip, str)
        if not marker_present(journal, _COMPLETE, b"complete\n", control_key):
            raise fail("TLS restore requires an authenticated completed forward generation")
        verify_rollback_tls_preimages(intent, context, journal)
        # A current strict receipt commits map/current ctime.  Its inverse is
        # therefore a whole-runtime transaction, never the historical
        # TLS-only exchange below.  The retained A receipt is the authenticated
        # discriminator; bootstrap has none and retains its no-prior outcome.
        if safe_leaf(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}", required_mode=0o644) is not None:
            return restore_v2_runtime_generation(generation, intent, context, journal, control_key)
        prior_identity = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
        current_identity = context.tls.chain[-1]
        rollback_complete = marker_present(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", control_key)
        if rollback_complete:
            if current_identity != old_identity or prior_identity != candidate_identity:
                raise fail("completed TLS rollback no longer owns its exact generations")
            verify_candidate_manifest(context.tls, previous_manifest, previous_lan_ip)
            prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=candidate_identity)
            try:
                verify_candidate_manifest(prior, candidate_manifest_value, lan_ip)
            finally:
                prior.close()
            if safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None:
                raise fail("completed TLS rollback has a public build receipt")
            return _restore_result(generation, old_identity, previous_manifest, candidate_identity, candidate_manifest_value, journal)
        if current_identity == old_identity and prior_identity == candidate_identity:
            # A kill after exchange but before the terminal marker is a known
            # inverse topology.  Re-prove it before recording completion.
            verify_candidate_manifest(context.tls, previous_manifest, previous_lan_ip)
            prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=candidate_identity)
            try:
                verify_candidate_manifest(prior, candidate_manifest_value, lan_ip)
            finally:
                prior.close()
            if safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None:
                raise fail("TLS rollback receipt state is ambiguous")
            marker(journal, _ROLLBACK_EXCHANGED, b"rollback-exchanged\n", context=context, control_key=control_key)
            marker(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", context=context, control_key=control_key)
            return _restore_result(generation, old_identity, previous_manifest, candidate_identity, candidate_manifest_value, journal)
        if current_identity != candidate_identity or prior_identity != old_identity:
            raise fail("TLS restore no longer owns its exact forward generations")
        # Mark intent before receipt invalidation.  The physical identities
        # below make a retry idempotent across every following crash boundary.
        marker(journal, _ROLLBACK_READY, b"rollback-ready\n", context=context, control_key=control_key)
        move_current_receipt_for_rollback(context, journal, control_key)
        context.assert_all(phase="immediately before TLS rollback exchange")
        journal.assert_current(phase="immediately before TLS rollback exchange")
        rename_exchange_between(
            context.runtime.descriptor,
            "tls",
            journal.descriptor,
            _PRIOR_TLS,
            label="TLS rollback exchange",
        )
        fsync(context.runtime.descriptor, label="runtime after TLS rollback exchange")
        fsync(journal.descriptor, label="TLS journal after TLS rollback exchange")
        now_current = child_identity(context.runtime, "tls", label="TLS public entry")
        now_prior = child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}")
        if now_current != old_identity or now_prior != candidate_identity:
            raise fail("TLS rollback directory changed during exchange")
        context.rebind_tls(old_identity)
        verify_candidate_manifest(context.tls, previous_manifest, previous_lan_ip)
        prior = open_private_dir(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}", expected=candidate_identity)
        try:
            verify_candidate_manifest(prior, candidate_manifest_value, lan_ip)
        finally:
            prior.close()
        if safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None:
            raise fail("TLS rollback left a public build receipt")
        marker(journal, _ROLLBACK_EXCHANGED, b"rollback-exchanged\n", context=context, control_key=control_key)
        marker(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", context=context, control_key=control_key)
        return _restore_result(generation, old_identity, previous_manifest, candidate_identity, candidate_manifest_value, journal)
    finally:
        journal.close()


def completed_unreceipted_inverse_generation(context: Context, control_key: bytes) -> str | None:
    """Find the one completed unreceipted inverse that owns current A.

    The discovery control can itself complete immediately before activation
    persists its checkpoint.  In that state a newly issued A receipt is
    public, so rejecting every public receipt would make a harmless replay
    strand the retained B generation.  This recognises only the exact durable
    topology produced by the unreceipted branch: a completed forward journal,
    a completed inverse, retained A receipt, current A TLS, and retained B
    TLS.  A failed runtime finalization may retain an unsigned B core under
    ``rollback-prior-build-receipt.json``; an authenticated B receipt instead
    identifies an ordinary explicit inverse and is skipped.  The unsigned
    core is checked against the journal's exact TLS and output postimages, and
    the normal restore routine re-validates every receipt/output identity
    before returning it to the caller.
    """
    try:
        names = tuple(os.listdir(context.runtime.descriptor))
    except OSError as exc:
        raise fail("could not enumerate TLS journals for unreceipted recovery", exc) from exc
    matches: list[str] = []
    for name in names:
        if re.fullmatch(re.escape(_JOURNAL_PREFIX) + r"[0-9a-f]{32}", name) is None:
            continue
        journal = open_private_dir(context.runtime, name, label="TLS exchange journal")
        try:
            intent = parse_intent(journal, control_key)
            generation = intent["id"]
            old_identity = intent["tls_previous"]
            candidate_identity = intent["candidate_identity"]
            previous_manifest = intent.get("tls_previous_manifest")
            assert isinstance(generation, str)
            assert isinstance(old_identity, tuple)
            assert isinstance(candidate_identity, tuple)
            if previous_manifest is None:
                continue
            if not (
                marker_present(journal, _COMPLETE, b"complete\n", control_key)
                and marker_present(journal, _ROLLBACK_COMPLETE, b"rollback-complete\n", control_key)
            ):
                continue
            rollback_receipt = safe_leaf(
                journal.descriptor,
                _ROLLBACK_PRIOR_RECEIPT,
                label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
                required_mode=0o644,
            )
            if rollback_receipt is not None:
                b_receipt, _b_receipt_identity, b_outputs, b_publication = read_authenticated_runtime_receipt(
                    journal,
                    _ROLLBACK_PRIOR_RECEIPT,
                    control_key,
                    label=f"TLS journal {_ROLLBACK_PRIOR_RECEIPT}",
                    allow_unattested=True,
                )
                b_current = b_publication["current"]
                b_prior = b_publication["prior"]
                assert isinstance(b_current, dict)
                if (
                    b_publication["generation"] != generation
                    or tuple(b_current["identity"]) != candidate_identity
                    or not isinstance(b_prior, dict)
                    or b_prior.get("journal") != journal.name
                    or tuple(b_prior.get("journal_identity", ())) != journal.identity
                    or b_prior.get("path") != _PRIOR_TLS
                    or tuple(b_prior.get("identity", ())) != old_identity
                ):
                    raise fail("retained B receipt does not bind the completed TLS inverse")
                if "producer_attestation" in b_receipt or "producer_commitment" in b_receipt:
                    # An authenticated B receipt identifies a normal explicit
                    # inverse.  Discovery must never replay that candidate.
                    continue
                b_outputs = runtime_output_expectations(b_receipt)
                for path, retained_name in (
                    ("current-lan-ip", _PRIOR_CURRENT),
                    ("nginx/allowed-hosts.map", _PRIOR_HOSTS),
                ):
                    expected, digest = b_outputs[path]
                    if not leaf_matches(
                        journal,
                        retained_name,
                        expected,
                        digest,
                        label=f"TLS journal {retained_name}",
                        allow_renamed=True,
                    ):
                        raise fail(f"retained B {path} does not match its receipt")
            if safe_leaf(journal.descriptor, _PRIOR_RECEIPT, label=f"TLS journal {_PRIOR_RECEIPT}", required_mode=0o644) is None:
                raise fail("completed unreceipted TLS inverse lost its retained A receipt")
            if (
                context.tls.chain[-1] != old_identity
                or child_identity(journal, _PRIOR_TLS, label=f"TLS journal {_PRIOR_TLS}") != candidate_identity
            ):
                continue
            matches.append(generation)
        finally:
            journal.close()
    if len(matches) > 1:
        raise fail("multiple completed unreceipted TLS inverses own the current generation")
    return matches[0] if matches else None


def open_current_tls_journal_for_restore(
    context: Context,
    control_key: bytes,
) -> PrivateDir | None:
    """Open the completed journal without trusting a possibly foreign receipt."""
    try:
        names = tuple(os.listdir(context.runtime.descriptor))
    except OSError as exc:
        raise fail("could not enumerate TLS journals for unreceipted restore", exc) from exc
    found: PrivateDir | None = None
    for name in names:
        if re.fullmatch(re.escape(_JOURNAL_PREFIX) + r"[0-9a-f]{32}", name) is None:
            continue
        journal = open_private_dir(context.runtime, name, label="TLS exchange journal for restore")
        keep = False
        try:
            intent = parse_intent(journal, control_key)
            candidate_identity = intent["candidate_identity"]
            assert isinstance(candidate_identity, tuple)
            if (
                candidate_identity != context.tls.chain[-1]
                or not marker_present(journal, _COMPLETE, b"complete\n", control_key)
            ):
                continue
            if found is not None:
                raise fail("multiple completed TLS journals own the current restore generation")
            found = journal
            keep = True
        finally:
            if not keep:
                journal.close()
    return found


def restore_unreceipted_tls_generation(context: Context, control_key: bytes) -> str:
    """Inverse the one proven B generation left before receipt publication.

    This control is intentionally narrower than normal recovery.  An outer
    activation that died after the forward TLS exchange has no B receipt from
    which to recover its generation token, but it must not guess from an
    arbitrary retained journal.  Only a single descriptor-validated completed
    journal that owns the current TLS directory, retains its signed A
    generation, and has no public receipt is eligible.  The activation layer
    calls it while holding its own serialized rollback lock.
    """
    public_receipt = safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT)
    if public_receipt is not None:
        completed = completed_unreceipted_inverse_generation(context, control_key)
        if completed is not None:
            try:
                result = json.loads(restore_tls_generation(completed, context, control_key))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise fail("completed unreceipted TLS restore produced invalid result", exc) from exc
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise fail("completed unreceipted TLS restore did not prove its inverse")
            result["discovered"] = True
            return json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        # The public leaf may be a same-mode replacement left by a failed
        # attestor.  Quarantine it before collecting TLS provenance so the
        # provenance check never parses an untrusted receipt as if it were A.
        foreign_journal = open_current_tls_journal_for_restore(context, control_key)
        if foreign_journal is None:
            raise fail("no authenticated unreceipted TLS generation is available")
        try:
            quarantine_public_runtime_receipt(context, foreign_journal)
        finally:
            foreign_journal.close()
        public_receipt = None
    try:
        publication = current_tls_publication(context, control_key)
    except NeedsRotation as exc:
        raise fail("no authenticated unreceipted TLS generation is available") from exc
    generation = publication["generation"]
    prior = publication["prior"]
    assert isinstance(generation, str)
    if prior is None:
        # Bootstrap has no authenticated A generation to restore.  Do not
        # path-delete B TLS merely because an outer deployment failed.
        return json.dumps(
            {
                "action": "no-prior-tls-generation",
                "discovered": True,
                "generation": generation,
                "ok": False,
                "prior": None,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    assert isinstance(prior, dict)
    journal_name = prior["journal"]
    journal_identity = tuple(prior["journal_identity"])
    assert isinstance(journal_name, str)
    journal = open_private_dir(
        context.runtime,
        journal_name,
        label="TLS exchange journal for unreceipted restore",
        expected=journal_identity,
    )
    try:
        intent = parse_intent(journal, control_key)
        if not adopt_failed_runtime_receipt(context, journal, intent, control_key):
            raise fail("no descriptor-bound failed runtime receipt is available")
        if public_receipt is not None:
            # A path/inode fault can leave a foreign replacement at the public
            # name.  Preserve that inode privately before the normal inverse
            # consumes the adopted B receipt.
            quarantine_public_runtime_receipt(context, journal)
    finally:
        journal.close()
    if safe_leaf(context.runtime.descriptor, _RECEIPT, label=_RECEIPT) is not None:
        raise fail("build receipt remained public after unreceipted recovery preflight")
    try:
        result = json.loads(restore_tls_generation(generation, context, control_key))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("TLS restore produced invalid result", exc) from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise fail("unreceipted TLS restore did not produce an inverse")
    result["discovered"] = True
    return json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def main() -> tuple[int, str | None]:
    if len(sys.argv) != 6:
        raise fail("invalid TLS exchange invocation")
    mode, runtime_path, serialized, lan_ip, work_name = sys.argv[1:]
    if mode not in {"recover", "validate", "publish", "provenance", "restore", "restore-unreceipted"}:
        raise fail("invalid TLS exchange mode")
    if mode not in {"restore", "restore-unreceipted"}:
        rfc1918_ipv4(lan_ip, label="TLS exchange LAN address")
    runtime = open_anchor(runtime_path, label="TermDeck runtime parent")
    tls = open_anchor(runtime_path + "/tls", label="TermDeck TLS runtime parent")
    nginx = open_anchor(runtime_path + "/nginx", label="TermDeck nginx runtime parent")
    binaries = open_anchor(runtime_path + "/bin", label="TermDeck binary runtime parent")
    control_parent = open_anchor(os.path.dirname(runtime_path), label="TermDeck TLS recovery control parent")
    context = Context(runtime, tls, nginx, binaries)
    try:
        bind_guards(serialized, context)
        control_key = recovery_key(control_parent, context)
        if mode == "recover":
            requires_rotation = recover_pending(context, control_key)
            return (2 if requires_rotation else 0), serialize_guards(context)
        if mode == "validate":
            try:
                validate_tls_directory(context.tls.descriptor, lan_ip, label="TLS runtime")
            except NeedsRotation:
                return 2, None
            context.assert_all(phase="after validating TLS bundle")
            return 0, None
        if mode == "restore":
            return 0, restore_tls_generation(work_name, context, control_key)
        if mode == "restore-unreceipted":
            if work_name:
                raise fail("unreceipted TLS restore does not accept a generation")
            return 0, restore_unreceipted_tls_generation(context, control_key)
        if mode == "provenance":
            if recover_pending(context, control_key):
                return 2, None
            try:
                publication = current_tls_publication(context, control_key)
            except NeedsRotation:
                return 2, None
            return 0, json.dumps(publication, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        recover_pending(context, control_key)
        if work_name:
            raise fail("TLS exchange does not accept a pathname rotation workspace")
        stage_exchange(context, lan_ip, control_key)
        validate_tls_directory(context.tls.descriptor, lan_ip, label="TLS runtime")
        context.assert_all(phase="after publishing TLS exchange")
        return 0, serialize_guards(context)
    finally:
        context.close()
        control_parent.close()


try:
    status, output = main()
    if output is not None:
        sys.stdout.write(output)
    raise SystemExit(status)
except TlsError as exc:
    sys.stderr.write(f"TermDeck TLS transaction failed: {exc}\n")
    raise SystemExit(1)


PY
}

if [ -n "$restore_generation" ]; then
    tls_transaction restore "$restore_generation"
    exit $?
fi
if [ "$restore_unreceipted" -eq 1 ]; then
    tls_transaction restore-unreceipted
    exit $?
fi

# A prior crash can leave the receipt intentionally absent with a durable TLS
# journal.  Recover that known transaction before deciding whether the current
# bundle is safe to reuse, and carry its re-bound TLS chain into the runtime
# publication helper below.
recover_status=0
if runtime_guards=$(tls_transaction recover); then
    :
else
    recover_status=$?
    if [ "$recover_status" -ne 2 ]; then
        exit "$recover_status"
    fi
fi

rotate=0
if [ "$recover_status" -eq 2 ]; then
    # A signed completed journal found target-leaf drift.  It cannot authorize
    # reuse even if a generic certificate check would pass, so publish a fresh
    # generation from its still-bound CA material.
    rotate=1
elif tls_transaction validate; then
    :
else
    tls_status=$?
    if [ "$tls_status" -eq 2 ]; then
        rotate=1
    else
        exit "$tls_status"
    fi
fi

if [ "$rotate" -eq 1 ]; then
    runtime_guards=$(tls_transaction publish)
fi

if tls_transaction validate; then
    :
else
    tls_status=$?
    echo "TermDeck TLS bundle failed post-publication verification" >&2
    exit "$tls_status"
fi

# Bind the build receipt to the exact authenticated TLS exchange that produced
# its served leaves.  Older/no-provenance TLS is migrated through one fresh
# generation before the receipt is allowed to become public.
if tls_publication=$(tls_transaction provenance); then
    :
else
    tls_status=$?
    if [ "$tls_status" -ne 2 ]; then
        exit "$tls_status"
    fi
    runtime_guards=$(tls_transaction publish)
    if tls_publication=$(tls_transaction provenance); then
        :
    else
        tls_status=$?
        exit "$tls_status"
    fi
fi

# Keep the LAN runtime receipt derived from the exact files that the service
# will use.  The receipt is written last, after TLS rotation and host-map
# generation, so an atomic update can never describe a partial runtime.  This
# helper uses only no-follow directory descriptors after the existing TLS work:
# the generated leaves cannot be redirected through a symlink, hard link, or a
# parent/leaf swap between validation and publication.
/usr/bin/python3 -I - "$runtime" "$lan_ip" "$runtime_guards" "$tls_publication" <<'PY'
from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys


class PublicationError(RuntimeError):
    pass


_NOREPLACE = 1
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
if not _DIRECTORY or not _NOFOLLOW:
    raise SystemExit("TermDeck runtime publication requires O_DIRECTORY and O_NOFOLLOW")
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_RECOVERY_KEY = ".termdeck-tls-recovery-key"
_RUNTIME_RECEIPT_SCHEMA = "termdeck.lan-runtime-build.v2"
_RUNTIME_RECEIPT_DOMAIN = b"termdeck.lan-runtime-build.v2\x00"
_ATTEST_HELPER = "/usr/libexec/termdeck-preparer-state"
_PRIOR_RECEIPT = "prior-build-receipt.json"
_PRIOR_CURRENT = "prior-current-lan-ip"
_PRIOR_HOSTS = "prior-allowed-hosts.map"


def _fail(message: str, exc: BaseException | None = None) -> PublicationError:
    if exc is None:
        return PublicationError(message)
    return PublicationError(f"{message}: {exc}")


def _run_attestation_helper(
    receipt_path: str,
    *,
    transaction_id: str | None = None,
    label: str,
) -> None:
    """Use the fixed helper for exact-inode signing or public-key verification."""
    arguments = [_ATTEST_HELPER, "--receipt", receipt_path]
    if transaction_id is None:
        arguments.append("--verify-existing")
    else:
        if re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
            raise _fail(f"{label} has an invalid attestation transaction")
        arguments.extend(("--transaction-id", transaction_id))
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            close_fds=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(f"{label} attestation helper failed", exc) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise _fail(
            f"{label} attestation helper refused the receipt"
            + (f": {detail}" if detail else "")
        )


def _component(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise _fail(f"unsafe {label} component: {value!r}")
    return value


def _absolute_components(path: str, *, label: str) -> tuple[str, ...]:
    if not path or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _fail(f"{label} is not an absolute canonical path: {path!r}")
    components = tuple(_component(part, label=label) for part in path.split("/")[1:])
    if not components:
        raise _fail(f"{label} must not be the filesystem root")
    return components


def _directory_identity(info: os.stat_result, *, label: str) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise _fail(f"{label} is not a real directory")
    return int(info.st_dev), int(info.st_ino), stat.S_IMODE(info.st_mode), int(info.st_uid)


def _file_identity(info: os.stat_result, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if info.st_ino <= 0 or info.st_nlink <= 0:
        raise _fail(f"{label} has an unsupported filesystem identity")
    return (
        int(info.st_dev), int(info.st_ino), stat.S_IMODE(info.st_mode), int(info.st_nlink),
        int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


def _same_object(left: tuple[int, int, int, int, int, int, int], right: tuple[int, int, int, int, int, int, int]) -> bool:
    return left[:4] == right[:4]


def _same_after_rename(left: tuple[int, int, int, int, int, int, int], right: tuple[int, int, int, int, int, int, int]) -> bool:
    # A rename updates ctime, while mode, links, size, and mtime must remain exact.
    return left[:6] == right[:6]


def _fsync(descriptor: int, *, label: str) -> None:
    for attempt in range(3):
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            if attempt == 2:
                break
        except OSError as exc:
            raise _fail(f"could not fsync {label}", exc) from exc
    raise _fail(f"could not fsync {label}: interrupted three times")


def _lstat_at(descriptor: int, name: str, *, label: str) -> os.stat_result | None:
    _component(name, label=label)
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _fail(f"could not inspect {label}", exc) from exc


def _trusted_leaf(descriptor: int, name: str, *, label: str) -> tuple[int, int, int, int, int, int, int] | None:
    info = _lstat_at(descriptor, name, label=label)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise _fail(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise _fail(f"{label} is not a regular file")
    identity = _file_identity(info, label=label)
    if identity[3] != 1:
        raise _fail(f"{label} has unexpected hard links")
    return identity


class Anchor:
    def __init__(self, path: str, descriptor: int, chain: tuple[tuple[int, int, int, int], ...], label: str) -> None:
        self.path = path
        self.descriptor = descriptor
        self.chain = chain
        self.label = label

    def assert_current(self, *, phase: str) -> None:
        try:
            current = _directory_identity(os.fstat(self.descriptor), label=self.label)
        except OSError as exc:
            raise _fail(f"{self.label} descriptor is unavailable {phase}", exc) from exc
        if current != self.chain[-1]:
            raise _fail(f"{self.label} identity changed {phase}")
        fresh = _open_anchor(self.path, label=self.label)
        try:
            if fresh.chain != self.chain:
                raise _fail(f"{self.label} identity changed {phase}")
        finally:
            fresh.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_anchor(path: str, *, label: str) -> Anchor:
    components = _absolute_components(path, label=label)
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail(f"could not open filesystem root for {label}", exc) from exc
    chain: list[tuple[int, int, int, int]] = []
    try:
        chain.append(_directory_identity(os.fstat(descriptor), label="filesystem root"))
        for component in components:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise _fail(f"{label} is not a real directory", exc) from exc
            os.close(descriptor)
            descriptor = child
            chain.append(_directory_identity(os.fstat(descriptor), label=label))
        return Anchor(path, descriptor, tuple(chain), label)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _load_producer_commitment_key(
    control_parent: Anchor,
    runtime: Anchor,
    *,
    anchors: tuple[Anchor, ...],
) -> bytes:
    """Read the TLS-created receipt authenticator without any pathname fallback.

    The key is intentionally outside the runtime directory so a writer that
    can replace public runtime leaves cannot manufacture a receipt.  The TLS
    transaction creates it once; publication only accepts an already-private,
    descriptor-rooted key and never self-heals a missing or malformed one.
    """
    owner = control_parent.chain[-1][3]

    def checked() -> tuple[int, int, int, int, int, int, int]:
        info = _lstat_at(control_parent.descriptor, _RECOVERY_KEY, label="runtime receipt authenticator")
        if (
            info is None
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != owner
            or info.st_nlink != 1
        ):
            raise _fail("runtime receipt authenticator is not a private regular file")
        return _file_identity(info, label="runtime receipt authenticator")

    _assert_all(anchors, phase="before loading runtime receipt authenticator")
    runtime.assert_current(phase="before loading runtime receipt authenticator")
    control_parent.assert_current(phase="before loading runtime receipt authenticator")
    before = checked()
    try:
        descriptor = os.open(_RECOVERY_KEY, _READ_FLAGS, dir_fd=control_parent.descriptor)
    except OSError as exc:
        raise _fail("could not securely open runtime receipt authenticator", exc) from exc
    try:
        opened = _file_identity(os.fstat(descriptor), label="runtime receipt authenticator")
        if opened != before:
            raise _fail("runtime receipt authenticator changed while opening")
        data = os.read(descriptor, 33)
        if len(data) != 32 or os.read(descriptor, 1):
            raise _fail("runtime receipt authenticator has an invalid length")
        after = _file_identity(os.fstat(descriptor), label="runtime receipt authenticator")
        if after != opened or checked() != before:
            raise _fail("runtime receipt authenticator changed while reading")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    control_parent.assert_current(phase="after loading runtime receipt authenticator")
    runtime.assert_current(phase="after loading runtime receipt authenticator")
    _assert_all(anchors, phase="after loading runtime receipt authenticator")
    return data


def _assert_all(anchors: tuple[Anchor, ...], *, phase: str) -> None:
    for anchor in anchors:
        anchor.assert_current(phase=phase)


def _guard_chain(value: object, *, label: str) -> tuple[tuple[int, int, int, int], ...]:
    if not isinstance(value, list) or not value:
        raise _fail(f"invalid directory-preflight guard for {label}")
    chain: list[tuple[int, int, int, int]] = []
    for identity in value:
        if (
            not isinstance(identity, list)
            or len(identity) != 4
            or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in identity)
        ):
            raise _fail(f"invalid directory-preflight guard for {label}")
        if identity[1] <= 0:
            raise _fail(f"invalid directory-preflight guard for {label}")
        chain.append((identity[0], identity[1], identity[2], identity[3]))
    return tuple(chain)


def _bind_preflight_guards(
    serialized: str,
    runtime_path: str,
    anchors: tuple[Anchor, ...],
) -> None:
    expected = {
        runtime_path: anchors[0],
        runtime_path + "/nginx": anchors[1],
        runtime_path + "/tls": anchors[2],
        runtime_path + "/bin": anchors[3],
    }
    try:
        supplied = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _fail("invalid directory-preflight guard payload", exc) from exc
    if not isinstance(supplied, dict) or set(supplied) != set(expected):
        raise _fail("invalid directory-preflight guard payload")
    for path, anchor in expected.items():
        if _guard_chain(supplied[path], label=anchor.label) != anchor.chain:
            raise _fail(f"{anchor.label} changed since directory preflight")
    _assert_all(anchors, phase="after binding directory-preflight guards")


def _renameat2(
    source_descriptor: int,
    source: str,
    destination_descriptor: int,
    destination: str,
    *,
    label: str,
) -> None:
    _component(source, label=label)
    _component(destination, label=label)
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise _fail("secure runtime publication requires renameat2(RENAME_NOREPLACE)", exc) from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    for attempt in range(3):
        result = renameat2(
            source_descriptor, os.fsencode(source), destination_descriptor, os.fsencode(destination), _NOREPLACE,
        )
        if result == 0:
            return
        failure = ctypes.get_errno()
        if failure != errno.EINTR:
            break
        if attempt == 2:
            raise _fail(f"secure rename for {label} was interrupted three times")
    if failure == errno.EEXIST:
        raise FileExistsError(failure, os.strerror(failure), destination)
    if failure == errno.ENOENT:
        raise FileNotFoundError(failure, os.strerror(failure), source)
    if failure in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
        raise _fail("secure runtime publication requires renameat2(RENAME_NOREPLACE)")
    raise _fail(f"secure rename failed for {label}", OSError(failure, os.strerror(failure)))


class PrivateStage:
    def __init__(self, parent: Anchor, name: str, descriptor: int, identity: tuple[int, int, int, int], label: str) -> None:
        self.parent = parent
        self.name = name
        self.descriptor = descriptor
        self.identity = identity
        self.label = label
        self.payload_descriptor = -1
        self.payload_identity: tuple[int, int, int, int, int, int, int] | None = None
        self.payload_digest: bytes | None = None

    def assert_current(self, *, phase: str) -> None:
        self.parent.assert_current(phase=phase)
        try:
            actual = _directory_identity(os.fstat(self.descriptor), label=f"{self.label} private stage")
        except OSError as exc:
            raise _fail(f"{self.label} private-stage descriptor is unavailable {phase}", exc) from exc
        named = _lstat_at(self.parent.descriptor, self.name, label=f"{self.label} private stage")
        owner = self.parent.chain[-1][3]
        if (
            self.identity[2] != 0o700
            or self.identity[3] != owner
            or named is None
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(named.st_mode) != 0o700
            or named.st_uid != owner
            or _directory_identity(named, label=f"{self.label} private stage") != self.identity
            or actual != self.identity
            or actual[2] != 0o700
            or actual[3] != owner
        ):
            raise _fail(f"{self.label} private stage changed {phase}")

    def write_payload(self, content: object, mode: int, *, anchors: tuple[Anchor, ...]) -> None:
        _assert_mutation_ready(anchors, phase=f"immediately before creating staged {self.label}", stage=self)
        try:
            descriptor = os.open("payload", _WRITE_FLAGS, mode, dir_fd=self.descriptor)
        except OSError as exc:
            raise _fail(f"could not create private staged {self.label}", exc) from exc
        self.payload_descriptor = descriptor
        try:
            _assert_mutation_ready(anchors, phase=f"immediately before setting staged {self.label} mode", stage=self)
            os.fchmod(descriptor, mode)
            if callable(content):
                # Receipt commitments bind the inode that will become the
                # public leaf.  dev/ino/mode/nlink are immutable across the
                # later write and no-replace rename; mtime/ctime are not.
                initial = _file_identity(os.fstat(descriptor), label=f"private staged {self.label}")
                if (
                    not stat.S_ISREG(os.fstat(descriptor).st_mode)
                    or initial[2] != mode
                    or initial[3] != 1
                    or initial[4] != 0
                ):
                    raise _fail(f"private staged {self.label} changed before payload construction")
                built = content(initial[:4])
                if not isinstance(built, bytes):
                    raise _fail(f"private staged {self.label} payload builder did not return bytes")
                payload = built
            elif isinstance(content, bytes):
                payload = content
            else:
                raise _fail(f"private staged {self.label} received invalid payload")
            offset = 0
            while offset < len(payload):
                _assert_mutation_ready(anchors, phase=f"immediately before writing staged {self.label}", stage=self)
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise _fail(f"could not write private staged {self.label}")
                offset += written
            _fsync(descriptor, label=f"private staged {self.label}")
            observed = _file_identity(os.fstat(descriptor), label=f"private staged {self.label}")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or observed[2] != mode or observed[3] != 1 or observed[4] != len(payload):
                raise _fail(f"private staged {self.label} changed while writing")
            named = _trusted_leaf(self.descriptor, "payload", label=f"private staged {self.label}")
            if named is None or not _same_object(named, observed):
                raise _fail(f"private staged {self.label} changed while writing")
            self.payload_identity = observed
            self.payload_digest = hashlib.sha256(payload).digest()
            _fsync(self.descriptor, label=f"private stage directory for {self.label}")
            _assert_all(anchors, phase=f"after staged write for {self.label}")
        except BaseException:
            self.close_payload()
            raise

    def verify_payload(self, *, anchors: tuple[Anchor, ...]) -> tuple[int, int, int, int, int, int, int]:
        if self.payload_descriptor < 0 or self.payload_identity is None or self.payload_digest is None:
            raise _fail(f"{self.label} has no staged payload")
        self.assert_current(phase="before staged publish")
        named = _trusted_leaf(self.descriptor, "payload", label=f"private staged {self.label}")
        opened = _file_identity(os.fstat(self.payload_descriptor), label=f"private staged {self.label}")
        if named is None or not _same_object(named, opened) or opened != self.payload_identity or opened[3] != 1:
            raise _fail(f"private staged {self.label} changed before publish")
        try:
            os.lseek(self.payload_descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while True:
                block = os.read(self.payload_descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            os.lseek(self.payload_descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise _fail(f"could not verify private staged {self.label}", exc) from exc
        after = _file_identity(os.fstat(self.payload_descriptor), label=f"private staged {self.label}")
        if digest.digest() != self.payload_digest or after != opened:
            raise _fail(f"private staged {self.label} changed before publish")
        _assert_all(anchors, phase=f"immediately before publish for {self.label}")
        return opened

    def close_payload(self) -> None:
        if self.payload_descriptor >= 0:
            descriptor, self.payload_descriptor = self.payload_descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass

    def discard_empty(self, *, anchors: tuple[Anchor, ...]) -> None:
        # Do not rmdir the stage through its public-parent name: another writer
        # could replace that name after the identity check.  First move the
        # verified stage into a freshly allocated private disposal directory
        # with RENAME_NOREPLACE, then revalidate the object through that open
        # descriptor and remove only its private child.  Any ambiguity retains
        # the moved object as recovery evidence.
        self.assert_current(phase="before private-stage cleanup")
        try:
            entries = os.listdir(self.descriptor)
        except OSError as exc:
            raise _fail(f"could not inspect private stage for {self.label}", exc) from exc
        if entries:
            raise _fail(f"private stage for {self.label} retains recovery evidence")
        disposal = _new_disposal(self.parent, label=self.label, anchors=anchors)
        try:
            _assert_mutation_ready(anchors, phase=f"immediately before private-stage isolation for {self.label}", stage=self)
            disposal.assert_current(phase="immediately before private-stage isolation")
            try:
                _renameat2(
                    self.parent.descriptor,
                    self.name,
                    disposal.descriptor,
                    "stage",
                    label=f"private-stage isolation for {self.label}",
                )
            except (FileExistsError, FileNotFoundError) as exc:
                raise _fail(f"could not isolate private stage for {self.label}", exc) from exc
            _fsync(self.parent.descriptor, label=f"publication parent after private-stage isolation for {self.label}")
            _fsync(disposal.descriptor, label=f"private disposal after isolating {self.label}")
            _assert_all(anchors, phase=f"after private-stage isolation for {self.label}")
            disposal.assert_current(phase="after private-stage isolation")
            isolated = _lstat_at(disposal.descriptor, "stage", label=f"isolated private stage for {self.label}")
            try:
                descriptor_identity = _directory_identity(os.fstat(self.descriptor), label=f"{self.label} private stage")
            except OSError as exc:
                raise _fail(f"{self.label} private-stage descriptor is unavailable during cleanup", exc) from exc
            if (
                isolated is None
                or not stat.S_ISDIR(isolated.st_mode)
                or _directory_identity(isolated, label=f"isolated private stage for {self.label}") != self.identity
                or descriptor_identity != self.identity
            ):
                raise _fail(f"{self.label} private stage changed before isolated cleanup")
            # Retain the isolated child as well as its disposal directory.
            # Even an rmdir relative to the disposal FD would have a final
            # identity-check/rmdir gap if a same-user writer reached that
            # private directory.  The object is now outside every public
            # publication name, mode 0700, and serves as the durable recovery
            # record instead of risking deletion of a foreign replacement.
            _fsync(disposal.descriptor, label=f"private disposal after retaining {self.label}")
            _fsync(self.parent.descriptor, label=f"publication parent after private-stage cleanup for {self.label}")
            _assert_all(anchors, phase=f"after private-stage cleanup for {self.label}")
        finally:
            disposal.close()

    def close(self) -> None:
        self.close_payload()
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass


def _assert_mutation_ready(
    anchors: tuple[Anchor, ...],
    *,
    phase: str,
    stage: PrivateStage | None = None,
) -> None:
    """Bind every mutation to the preflight parent chains immediately before it."""
    _assert_all(anchors, phase=phase)
    if stage is not None:
        stage.assert_current(phase=phase)


def _new_stage(
    parent: Anchor,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
    prefix: str = ".termdeck-runtime-stage-",
) -> PrivateStage:
    _assert_all(anchors, phase=f"before private-stage creation for {label}")
    parent.assert_current(phase=f"immediately before private-stage creation for {label}")
    for _attempt in range(32):
        name = f"{prefix}{secrets.token_hex(16)}"
        _assert_all(anchors, phase=f"immediately before creating private stage for {label}")
        parent.assert_current(phase=f"immediately before creating private stage for {label}")
        try:
            os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _fail(f"could not create private stage for {label}", exc) from exc
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.descriptor)
        except OSError as exc:
            raise _fail(f"could not open private stage for {label}", exc) from exc
        identity = _directory_identity(os.fstat(descriptor), label=f"private stage for {label}")
        stage = PrivateStage(parent, name, descriptor, identity, label)
        try:
            stage.assert_current(phase="after creation")
            _fsync(parent.descriptor, label=f"publication parent after private-stage creation for {label}")
            _assert_all(anchors, phase=f"after private-stage creation for {label}")
            return stage
        except BaseException:
            stage.close()
            raise
    raise _fail(f"could not allocate a private stage for {label}")


def _new_disposal(parent: Anchor, *, label: str, anchors: tuple[Anchor, ...]) -> PrivateStage:
    """Allocate an empty private directory used only to isolate stage cleanup."""
    _assert_all(anchors, phase=f"before private-disposal creation for {label}")
    parent.assert_current(phase=f"immediately before private-disposal creation for {label}")
    for _attempt in range(32):
        name = f".termdeck-runtime-dispose-{secrets.token_hex(16)}"
        _assert_all(anchors, phase=f"immediately before creating private disposal for {label}")
        parent.assert_current(phase=f"immediately before creating private disposal for {label}")
        try:
            os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _fail(f"could not create private disposal for {label}", exc) from exc
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.descriptor)
        except OSError as exc:
            raise _fail(f"could not open private disposal for {label}", exc) from exc
        identity = _directory_identity(os.fstat(descriptor), label=f"private disposal for {label}")
        disposal = PrivateStage(parent, name, descriptor, identity, f"private disposal for {label}")
        try:
            disposal.assert_current(phase="after creation")
            try:
                entries = os.listdir(descriptor)
            except OSError as exc:
                raise _fail(f"could not inspect private disposal for {label}", exc) from exc
            if entries:
                raise _fail(f"private disposal for {label} was not empty after creation")
            _fsync(parent.descriptor, label=f"publication parent after private-disposal creation for {label}")
            _assert_all(anchors, phase=f"after private-disposal creation for {label}")
            return disposal
        except BaseException:
            disposal.close()
            raise
    raise _fail(f"could not allocate a private disposal for {label}")


def _restore_held_foreign_regular(
    stage: PrivateStage,
    parent: Anchor,
    name: str,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
) -> bool:
    """Restore only the FD-held foreign regular file into an empty public name.

    A simple lstat-then-rename of ``stage/prior`` has a final replacement
    race: a same-UID writer can swap that name and make us publish a different
    object.  Hold the verified original FD, make a private hard-link through
    ``/proc/self/fd`` (which resolves that FD, never the mutable name), then
    no-replace move the link.  If any step is unavailable, retain every entry
    privately and fail closed instead of publishing a substitute.
    """
    _assert_all(anchors, phase=f"before restoring retained {label}")
    stage.assert_current(phase="before restoring retained foreign leaf")
    if _lstat_at(parent.descriptor, name, label=label) is not None:
        return False
    expected = _trusted_leaf(stage.descriptor, "prior", label=f"private tombstone for {label}")
    if expected is None:
        return False
    try:
        descriptor = os.open("prior", _READ_FLAGS, dir_fd=stage.descriptor)
    except OSError as exc:
        raise _fail(f"could not securely hold retained {label}", exc) from exc
    try:
        held = _file_identity(os.fstat(descriptor), label=f"retained {label}")
        if not _same_object(held, expected) or held[2] != expected[2]:
            return False
        _assert_mutation_ready(anchors, phase=f"immediately before restoring retained {label}", stage=stage)
        # Link the held descriptor directly into the public empty name.  A
        # private temporary followed by rename has its own final replacement
        # race; link(2) gives us the required no-replace publication in one
        # syscall and never dereferences a mutable source pathname.
        try:
            os.link(
                f"/proc/self/fd/{descriptor}",
                name,
                dst_dir_fd=parent.descriptor,
                follow_symlinks=True,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise _fail(f"could not restore held retained {label}", exc) from exc
        restored = _lstat_at(parent.descriptor, name, label=label)
        if (
            restored is None
            or stat.S_ISLNK(restored.st_mode)
            or not stat.S_ISREG(restored.st_mode)
            or (int(restored.st_dev), int(restored.st_ino), stat.S_IMODE(restored.st_mode))
            != (held[0], held[1], held[2])
        ):
            raise _fail(f"retained {label} changed during restoration")
        _fsync(stage.descriptor, label=f"private stage after restoring retained {label}")
        _fsync(parent.descriptor, label=f"publication parent after restoring retained {label}")
        _assert_all(anchors, phase=f"after restoring retained {label}")
        return True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _take_trusted_prior(
    parent: Anchor,
    name: str,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
    expected: tuple[int, int, int, int, int, int, int] | None = None,
    require_absent: bool = False,
) -> tuple[PrivateStage, tuple[int, int, int, int, int, int, int]] | None:
    """Durably remove a trusted managed leaf before related files change.

    The caller owns the returned private stage.  It must either dispose that
    verified tombstone after the replacement succeeds or leave it in place as
    recovery evidence when a later operation fails.
    """
    _component(name, label=label)
    _assert_all(anchors, phase=f"before invalidating {label}")
    observed = _trusted_leaf(parent.descriptor, name, label=label)
    if require_absent:
        if expected is not None or observed is not None:
            raise _fail(f"{label} appeared before secure tombstone")
        return None
    if expected is None:
        expected = observed
    elif observed != expected:
        raise _fail(f"{label} changed before secure tombstone")
    if expected is None:
        return None
    stage = _new_stage(
        parent,
        label=label,
        anchors=anchors,
        prefix=".termdeck-runtime-transaction-",
    )
    try:
        _assert_mutation_ready(anchors, phase=f"immediately before invalidating {label}", stage=stage)
        try:
            _renameat2(parent.descriptor, name, stage.descriptor, "prior", label=f"tombstone {label}")
        except FileNotFoundError as exc:
            raise _fail(f"{label} changed before secure tombstone", exc) from exc
        prior_info = _lstat_at(stage.descriptor, "prior", label=f"private tombstone for {label}")
        prior = _file_identity(prior_info, label=f"private tombstone for {label}") if prior_info is not None else None
        if (
            prior_info is None
            or not stat.S_ISREG(prior_info.st_mode)
            or prior is None
            or prior[3] != 1
            or not _same_after_rename(prior, expected)
        ):
            # Preserve a foreign winner at its public name only when its held
            # regular-file descriptor can be re-linked and no-replace moved.
            # Otherwise leave all evidence private and fail closed.
            restored = _restore_held_foreign_regular(
                stage,
                parent,
                name,
                label=label,
                anchors=anchors,
            )
            if restored:
                raise _fail(f"{label} changed before secure tombstone; restored held foreign leaf")
            raise _fail(f"{label} changed before secure tombstone; retained ambiguous tombstone")
        _fsync(stage.descriptor, label=f"private stage after invalidating {label}")
        _fsync(parent.descriptor, label=f"publication parent after invalidating {label}")
        _assert_all(anchors, phase=f"after invalidating {label}")
        return stage, expected
    except BaseException:
        stage.close()
        raise


def _retain_trusted_prior(
    stage: PrivateStage,
    expected: tuple[int, int, int, int, int, int, int],
    *,
    anchors: tuple[Anchor, ...],
) -> None:
    """Move a verified prior into private retention without unlinking by name."""
    stage.assert_current(phase="before tombstone disposal")
    prior = _trusted_leaf(stage.descriptor, "prior", label=f"private tombstone for {stage.label}")
    if prior is None or not _same_after_rename(prior, expected):
        raise _fail(f"private tombstone for {stage.label} changed before disposal")
    retention = _new_disposal(stage.parent, label=f"retained tombstone for {stage.label}", anchors=anchors)
    try:
        _assert_mutation_ready(anchors, phase=f"immediately before retaining tombstone for {stage.label}", stage=stage)
        retention.assert_current(phase="immediately before retaining tombstone")
        try:
            _renameat2(
                stage.descriptor,
                "prior",
                retention.descriptor,
                "prior",
                label=f"retain tombstone for {stage.label}",
            )
        except (FileExistsError, FileNotFoundError) as exc:
            raise _fail(f"could not retain verified private tombstone for {stage.label}", exc) from exc
        _fsync(stage.descriptor, label=f"private stage after retaining tombstone for {stage.label}")
        _fsync(retention.descriptor, label=f"private retention after retaining tombstone for {stage.label}")
        _fsync(stage.parent.descriptor, label=f"publication parent after retaining tombstone for {stage.label}")
        _assert_all(anchors, phase=f"after retaining tombstone for {stage.label}")
        retention.assert_current(phase="after retaining tombstone")
        retained = _trusted_leaf(retention.descriptor, "prior", label=f"private retained tombstone for {stage.label}")
        if retained is None or not _same_after_rename(retained, expected):
            raise _fail(f"private tombstone for {stage.label} changed while retaining it")
    finally:
        retention.close()
    stage.discard_empty(anchors=anchors)


class ReceiptPublication:
    """Hold the receipt inode before its rename, including interruption windows."""

    def __init__(self) -> None:
        self.descriptor = -1
        self.staged: tuple[int, int, int, int, int, int, int] | None = None
        self.published: tuple[int, int, int, int, int, int, int] | None = None
        self.digest: bytes | None = None

    def bind(self, stage: PrivateStage) -> None:
        if self.descriptor >= 0 or stage.payload_descriptor < 0:
            raise _fail("invalid receipt publication ownership state")
        self.descriptor = os.dup(stage.payload_descriptor)
        self.staged = stage.payload_identity
        self.digest = stage.payload_digest

    def rebind_finalized(
        self,
        runtime: Anchor,
        *,
        anchors: tuple[Anchor, ...],
    ) -> tuple[int, int, int, int, int, int, int]:
        """Adopt final bytes only when the helper retained the published inode."""
        if self.descriptor < 0 or self.published is None:
            raise _fail("receipt publication was not bound before finalization")
        _assert_all(anchors, phase="after receipt attestation finalization")
        held = _file_identity(os.fstat(self.descriptor), label="held finalized receipt")
        named = _trusted_leaf(runtime.descriptor, "build-receipt.json", label="build-receipt.json")
        if (
            named != held
            or held[:4] != self.published[:4]
            or held[2] != 0o644
            or held[3] != 1
        ):
            raise _fail("receipt attestation did not finalize the published inode")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            block = os.read(self.descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if _file_identity(os.fstat(self.descriptor), label="held finalized receipt") != held:
            raise _fail("finalized receipt changed while rebinding publication ownership")
        self.published = held
        self.digest = digest.digest()
        _fsync(runtime.descriptor, label="publication parent after receipt finalization")
        return held

    def invalidate(self, runtime: Anchor, *, anchors: tuple[Anchor, ...]) -> None:
        if self.descriptor < 0 or self.staged is None or self.digest is None:
            return
        observed = _trusted_leaf(runtime.descriptor, "build-receipt.json", label="build-receipt.json")
        if observed is None:
            return
        held = _file_identity(os.fstat(self.descriptor), label="held published receipt")
        if not _same_object(observed, held):
            # The attestor may have replaced or renamed the canonical leaf
            # while it was finalizing the exact descriptor we supplied.  The
            # held descriptor still identifies our receipt, so recover that
            # inode by scanning only the anchored runtime directory, opening
            # each matching name, and moving the FD-bound name into a private
            # transaction stage.  A pathname-only lookup would accept the
            # replacement and lose the failed publication evidence.
            stage = _new_stage(
                runtime,
                label="displaced build-receipt.json",
                anchors=anchors,
                prefix=".termdeck-runtime-transaction-",
            )
            try:
                try:
                    names = tuple(os.listdir(runtime.descriptor))
                except OSError as exc:
                    raise _fail("could not enumerate displaced build receipt names", exc) from exc
                recovered = False
                for name in names:
                    if name == "build-receipt.json":
                        continue
                    candidate = _lstat_at(runtime.descriptor, name, label="displaced build receipt")
                    if candidate is None or stat.S_ISLNK(candidate.st_mode) or not stat.S_ISREG(candidate.st_mode):
                        continue
                    candidate_identity = _file_identity(candidate, label="displaced build receipt")
                    if not _same_object(candidate_identity, held) or candidate_identity[3] != 1:
                        continue
                    try:
                        candidate_fd = os.open(name, _READ_FLAGS, dir_fd=runtime.descriptor)
                    except FileNotFoundError:
                        continue
                    try:
                        opened = _file_identity(os.fstat(candidate_fd), label="displaced build receipt")
                        if not _same_object(opened, held) or opened[3] != 1:
                            continue
                        _assert_mutation_ready(
                            anchors,
                            phase="immediately before retaining displaced build receipt",
                            stage=stage,
                        )
                        try:
                            _renameat2(
                                runtime.descriptor,
                                name,
                                stage.descriptor,
                                "prior",
                                label="retain displaced build receipt",
                            )
                        except FileNotFoundError:
                            continue
                        retained = _trusted_leaf(
                            stage.descriptor,
                            "prior",
                            label="private displaced build receipt",
                        )
                        if retained is None or not _same_after_rename(retained, held):
                            raise _fail("displaced build receipt changed while retaining its owned inode")
                        _fsync(stage.descriptor, label="private stage after retaining displaced build receipt")
                        _fsync(runtime.descriptor, label="runtime after retaining displaced build receipt")
                        _assert_all(anchors, phase="after retaining displaced build receipt")
                        recovered = True
                        break
                    finally:
                        os.close(candidate_fd)
                if not recovered:
                    raise _fail("build-receipt.json is no longer the owned publication; retained public evidence")
            except BaseException:
                stage.close()
                raise
            finally:
                if stage.descriptor >= 0:
                    stage.close()
            return
        # An interruption can arrive after rename(2) or during the helper's
        # in-place final write.  The duplicated descriptor and exact named
        # inode prove this is still our publication even when its bytes are an
        # unsigned core or a partial attestation postimage.  Withdraw that
        # inode into private retention so rollback can proceed while preserving
        # the failed bytes as evidence.
        if _file_identity(os.fstat(self.descriptor), label="held published receipt") != held:
            raise _fail("held published receipt changed; retained public evidence")
        displaced = _take_trusted_prior(
            runtime,
            "build-receipt.json",
            label="build-receipt.json",
            anchors=anchors,
            expected=observed,
        )
        if displaced is not None:
            displaced[0].close()

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish(
    parent: Anchor,
    name: str,
    content: object,
    mode: int,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
    before_publication: object | None = None,
    require_absent: bool = False,
    expected: tuple[int, int, int, int, int, int, int] | None = None,
    receipt_publication: ReceiptPublication | None = None,
) -> tuple[int, int, int, int, int, int, int]:
    _component(name, label=label)
    _assert_all(anchors, phase=f"before preflight for {label}")
    observed = _trusted_leaf(parent.descriptor, name, label=label)
    if require_absent and (expected is not None or observed is not None):
        raise _fail(f"{label} appeared before no-replace publication")
    if observed != expected:
        raise _fail(f"{label} changed since managed receipt validation")
    stage = _new_stage(parent, label=label, anchors=anchors)
    try:
        stage.write_payload(content, mode, anchors=anchors)
        staged = stage.verify_payload(anchors=anchors)
        if receipt_publication is not None:
            receipt_publication.bind(stage)
        if expected is None:
            if before_publication is not None:
                if not callable(before_publication):
                    raise _fail(f"invalid pre-publication verifier for {label}")
                before_publication()
            _assert_mutation_ready(anchors, phase=f"immediately before no-replace publication for {label}", stage=stage)
            try:
                _renameat2(stage.descriptor, "payload", parent.descriptor, name, label=label)
            except FileExistsError as exc:
                raise _fail(f"{label} changed before no-replace publication", exc) from exc
            published = _trusted_leaf(parent.descriptor, name, label=label)
            if published is None or not _same_after_rename(published, staged):
                raise _fail(f"{label} changed during no-replace publication")
            if receipt_publication is not None:
                receipt_publication.published = published
            stage.close_payload()
            _fsync(stage.descriptor, label=f"private stage after publishing {label}")
            _fsync(parent.descriptor, label=f"publication parent after publishing {label}")
            _assert_all(anchors, phase=f"after publishing {label}")
            stage.discard_empty(anchors=anchors)
            return published

        # A replacement is two no-replace moves.  The old leaf first becomes a
        # private tombstone; if an intervening writer won the stale-name race,
        # that untrusted object is moved back only into an empty public name.
        _assert_mutation_ready(anchors, phase=f"immediately before tombstoning {label}", stage=stage)
        try:
            _renameat2(parent.descriptor, name, stage.descriptor, "prior", label=f"tombstone {label}")
        except FileNotFoundError as exc:
            raise _fail(f"{label} changed before secure tombstone", exc) from exc
        prior_info = _lstat_at(stage.descriptor, "prior", label=f"private tombstone for {label}")
        prior = _file_identity(prior_info, label=f"private tombstone for {label}") if prior_info is not None else None
        if (
            prior_info is None
            or not stat.S_ISREG(prior_info.st_mode)
            or prior is None
            or prior[3] != 1
            or not _same_after_rename(prior, expected)
        ):
            restored = _restore_held_foreign_regular(
                stage,
                parent,
                name,
                label=label,
                anchors=anchors,
            )
            if restored:
                raise _fail(f"{label} changed before secure tombstone; restored held foreign leaf")
            raise _fail(f"{label} changed before secure tombstone; retained ambiguous tombstone")
        _fsync(stage.descriptor, label=f"private stage after tombstoning {label}")
        _fsync(parent.descriptor, label=f"publication parent after tombstoning {label}")
        _assert_all(anchors, phase=f"after tombstoning {label}")
        stage.verify_payload(anchors=anchors)
        if before_publication is not None:
            if not callable(before_publication):
                raise _fail(f"invalid pre-publication verifier for {label}")
            before_publication()
        _assert_mutation_ready(anchors, phase=f"immediately before replacement publication for {label}", stage=stage)
        try:
            _renameat2(stage.descriptor, "payload", parent.descriptor, name, label=f"replacement {label}")
        except BaseException as exc:
            try:
                _restore_held_foreign_regular(stage, parent, name, label=label, anchors=anchors)
            except BaseException as cleanup_exc:
                exc.add_note(f"could not restore retained {label}: {cleanup_exc}")
            raise
        published = _trusted_leaf(parent.descriptor, name, label=label)
        if published is None or not _same_after_rename(published, staged):
            raise _fail(f"{label} changed during replacement publication")
        if receipt_publication is not None:
            receipt_publication.published = published
        stage.close_payload()
        _fsync(stage.descriptor, label=f"private stage after publishing {label}")
        _fsync(parent.descriptor, label=f"publication parent after publishing {label}")
        _assert_all(anchors, phase=f"after publishing {label}")
        prior_now = _trusted_leaf(stage.descriptor, "prior", label=f"private tombstone for {label}")
        if prior_now is None or not _same_after_rename(prior_now, expected):
            raise _fail(f"private tombstone for {label} changed before disposal")
        _retain_trusted_prior(stage, expected, anchors=anchors)
        return published
    finally:
        stage.close()


def _read_regular(anchor: Anchor, name: str, *, label: str, anchors: tuple[Anchor, ...]) -> bytes:
    _assert_all(anchors, phase=f"before reading {label}")
    before = _lstat_at(anchor.descriptor, name, label=label)
    if before is None or stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _fail(f"TermDeck receipt requires a regular file: {anchor.path}/{name}")
    before_identity = _file_identity(before, label=label)
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=anchor.descriptor)
    except OSError as exc:
        raise _fail(f"could not securely open {label}", exc) from exc
    try:
        opened = _file_identity(os.fstat(descriptor), label=label)
        if not _same_object(opened, before_identity):
            raise _fail(f"{label} changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > 16 * 1024 * 1024:
                raise _fail(f"{label} exceeds the 16 MiB receipt limit")
            chunks.append(block)
        after = _file_identity(os.fstat(descriptor), label=label)
        if after != opened:
            raise _fail(f"{label} changed while reading")
        named_after = _lstat_at(anchor.descriptor, name, label=label)
        if (
            named_after is None
            or stat.S_ISLNK(named_after.st_mode)
            or not stat.S_ISREG(named_after.st_mode)
            or _file_identity(named_after, label=label) != before_identity
        ):
            raise _fail(f"{label} changed while reading")
        _assert_all(anchors, phase=f"after reading {label}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


_TLS_POSTIMAGE_MODES = {
    "server.key": 0o600,
    "server.crt": 0o644,
    "server-chain.crt": 0o644,
    "server.ext": 0o600,
}


def _tls_extension(lan_ip: str) -> bytes:
    return (
        "[v3_req]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=@alt_names\n"
        "[alt_names]\n"
        "DNS.1=termdeck.local\n"
        "DNS.2=max.local\n"
        f"IP.1={lan_ip}\n"
        "IP.2=127.0.0.1\n"
    ).encode("ascii")


def _capture_tls_postimages(
    tls: Anchor,
    lan_ip: str,
    *,
    anchors: tuple[Anchor, ...],
) -> tuple[
    dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    dict[str, bytes],
]:
    """Capture every active TLS leaf through its anchored directory FD.

    This transient manifest deliberately includes the private key identity and
    digest so a key swap cannot race receipt publication, but it is never
    emitted in the public build receipt.
    """
    required = ("ca.crt", "ca.key", "server.key", "server.crt", "server-chain.crt", "server.ext")
    result: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] = {}
    contents: dict[str, bytes] = {}
    _assert_all(anchors, phase="before capturing TLS postimages")
    for name in required:
        label = f"tls/{name}"
        identity = _trusted_leaf(tls.descriptor, name, label=label)
        if identity is None:
            raise _fail(f"TermDeck TLS contract requires {label}")
        required_mode = _TLS_POSTIMAGE_MODES.get(name)
        if required_mode is not None and identity[2] != required_mode:
            raise _fail(f"TermDeck TLS contract requires mode {required_mode:04o} for {label}")
        data = _read_regular(tls, name, label=label, anchors=anchors)
        after = _trusted_leaf(tls.descriptor, name, label=label)
        if after != identity:
            raise _fail(f"TermDeck TLS postimage changed while capturing {label}")
        result[name] = (identity, hashlib.sha256(data).hexdigest())
        contents[name] = data
    # ``ca.srl`` is not served, but binds the CA's next serial allocation to
    # the same snapshot when it exists.  Its absence is an allowed legacy
    # state and no digest ever enters the public receipt.
    serial_identity = _trusted_leaf(tls.descriptor, "ca.srl", label="tls/ca.srl")
    if serial_identity is not None:
        serial = _read_regular(tls, "ca.srl", label="tls/ca.srl", anchors=anchors)
        if _trusted_leaf(tls.descriptor, "ca.srl", label="tls/ca.srl") != serial_identity:
            raise _fail("TermDeck TLS postimage changed while capturing tls/ca.srl")
        result["ca.srl"] = (serial_identity, hashlib.sha256(serial).hexdigest())
        contents["ca.srl"] = serial
    if (
        not contents["ca.crt"]
        or not contents["ca.key"]
        or contents["server-chain.crt"] != contents["server.crt"] + contents["ca.crt"]
        or contents["server.ext"] != _tls_extension(lan_ip)
    ):
        raise _fail("TermDeck TLS contract has an invalid chain or extension")
    _assert_all(anchors, phase="after capturing TLS postimages")
    return result, contents


def _open_tls_postimage(
    tls: Anchor,
    name: str,
    expected: tuple[int, int, int, int, int, int, int],
    *,
    anchors: tuple[Anchor, ...],
) -> int:
    label = f"tls/{name}"
    _assert_all(anchors, phase=f"before opening TLS postimage {label}")
    named = _trusted_leaf(tls.descriptor, name, label=label)
    if named != expected:
        raise _fail(f"TermDeck TLS postimage changed before opening {label}")
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=tls.descriptor)
    except OSError as exc:
        raise _fail(f"could not securely open {label}", exc) from exc
    try:
        opened = _file_identity(os.fstat(descriptor), label=label)
        if opened != expected:
            raise _fail(f"TermDeck TLS postimage changed while opening {label}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _openssl_tls(
    arguments: list[str],
    descriptors: tuple[int, ...],
    *,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["/usr/bin/openssl", *arguments],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            pass_fds=descriptors,
        )
    except OSError as exc:
        raise _fail("could not execute openssl for TermDeck TLS postimage validation", exc) from exc


def _validate_tls_postimages(
    tls: Anchor,
    lan_ip: str,
    *,
    anchors: tuple[Anchor, ...],
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str]]:
    """Validate the live TLS key/certificate/chain contract by held FDs."""
    expected, _contents = _capture_tls_postimages(tls, lan_ip, anchors=anchors)
    descriptors: list[int] = []
    try:
        for name in ("ca.crt", "ca.key", "server.key", "server.crt"):
            descriptors.append(_open_tls_postimage(tls, name, expected[name][0], anchors=anchors))
        ca_fd, ca_key_fd, key_fd, cert_fd = descriptors
        passed = tuple(descriptors)
        expiry = _openssl_tls(["x509", "-in", f"/proc/self/fd/{cert_fd}", "-noout", "-checkend", "2592000"], passed)
        verified = _openssl_tls(["verify", "-CAfile", f"/proc/self/fd/{ca_fd}", f"/proc/self/fd/{cert_fd}"], passed)
        key_public = _openssl_tls(["pkey", "-in", f"/proc/self/fd/{key_fd}", "-pubout", "-outform", "DER"], passed)
        cert_pem = _openssl_tls(["x509", "-in", f"/proc/self/fd/{cert_fd}", "-pubkey", "-noout"], passed)
        cert_public = _openssl_tls(["pkey", "-pubin", "-pubout", "-outform", "DER"], passed, input_data=cert_pem.stdout)
        ca_key_public = _openssl_tls(["pkey", "-in", f"/proc/self/fd/{ca_key_fd}", "-pubout", "-outform", "DER"], passed)
        ca_pem = _openssl_tls(["x509", "-in", f"/proc/self/fd/{ca_fd}", "-pubkey", "-noout"], passed)
        ca_public = _openssl_tls(["pkey", "-pubin", "-pubout", "-outform", "DER"], passed, input_data=ca_pem.stdout)
        san = _openssl_tls(["x509", "-in", f"/proc/self/fd/{cert_fd}", "-noout", "-ext", "subjectAltName"], passed)
        if any(
            result.returncode != 0
            for result in (expiry, verified, key_public, cert_pem, cert_public, ca_key_public, ca_pem, ca_public, san)
        ):
            raise _fail("TermDeck TLS postimage certificate contract failed")
        expected_san = [
            "DNS:termdeck.local",
            "DNS:max.local",
            f"IP Address:{lan_ip}",
            "IP Address:127.0.0.1",
        ]
        observed_san = re.findall(r"(?:DNS:[^,\s]+|IP Address:[^,\s]+)", san.stdout.decode("ascii", "replace"))
        if (
            key_public.stdout != cert_public.stdout
            or ca_key_public.stdout != ca_public.stdout
            or observed_san != expected_san
        ):
            raise _fail("TermDeck TLS postimage key, CA, or SAN contract failed")
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
    observed, _contents = _capture_tls_postimages(tls, lan_ip, anchors=anchors)
    if observed != expected:
        raise _fail("TermDeck TLS postimages changed while validating")
    return expected


def _verify_tls_postimages(
    tls: Anchor,
    lan_ip: str,
    expected: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    *,
    anchors: tuple[Anchor, ...],
) -> None:
    observed = _validate_tls_postimages(tls, lan_ip, anchors=anchors)
    if observed != expected:
        raise _fail("TermDeck TLS postimages changed before publication")


def _capture_source_postimages(
    sources: tuple[tuple[str, Anchor, str], ...],
    *,
    anchors: tuple[Anchor, ...],
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], str]]:
    """Read receipt sources as stable descriptor-observed postimages."""
    result: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]] = {}
    _assert_all(anchors, phase="before capturing receipt source postimages")
    for relative, anchor, name in sources:
        before = _trusted_leaf(anchor.descriptor, name, label=relative)
        if before is None:
            raise _fail(f"TermDeck receipt requires a regular file: {relative}")
        data = _read_regular(anchor, name, label=relative, anchors=anchors)
        after = _trusted_leaf(anchor.descriptor, name, label=relative)
        if after != before:
            raise _fail(f"receipt source changed while capturing {relative}")
        result[relative] = (before, hashlib.sha256(data).hexdigest())
    _assert_all(anchors, phase="after capturing receipt source postimages")
    return result


def _verify_source_postimages(
    sources: tuple[tuple[str, Anchor, str], ...],
    expected: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    *,
    anchors: tuple[Anchor, ...],
) -> None:
    observed = _capture_source_postimages(sources, anchors=anchors)
    if observed != expected:
        raise _fail("TermDeck receipt source postimages changed before publication")


_RECEIPT_SOURCE_PATHS = {
    "bin/termdeck-web-server",
    "nginx/nginx.conf",
    "tls/ca.crt",
    "tls/server-chain.crt",
    "tls/server.crt",
}


def _receipt_immutable_identity(value: object) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[2] != 0o644
        or value[3] != 1
    ):
        raise _fail("build-receipt.json has an invalid producer receipt identity")
    return tuple(value)  # type: ignore[return-value]


def _receipt_source_inputs(value: object, files: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or not isinstance(files, dict) or set(value) != _RECEIPT_SOURCE_PATHS:
        raise _fail("build-receipt.json has invalid producer source inputs")
    parsed: dict[str, dict[str, object]] = {}
    for path in sorted(_RECEIPT_SOURCE_PATHS):
        entry = value[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "sha256"}:
            raise _fail("build-receipt.json has invalid producer source inputs")
        identity = _publication_identity(entry["identity"], label=path)
        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or digest != files[path]
        ):
            raise _fail("build-receipt.json has invalid producer source inputs")
        parsed[path] = {"identity": list(identity), "sha256": digest}
    return parsed


def _producer_commitment(receipt: dict[str, object]) -> dict[str, object]:
    commitment = receipt.get("producer_commitment")
    if not isinstance(commitment, dict) or set(commitment) != {"algorithm", "receipt_identity", "source_inputs", "mac"}:
        raise _fail("build-receipt.json has an invalid producer commitment")
    if commitment.get("algorithm") != "hmac-sha256-v1":
        raise _fail("build-receipt.json has an invalid producer commitment")
    identity = _receipt_immutable_identity(commitment.get("receipt_identity"))
    inputs = _receipt_source_inputs(commitment.get("source_inputs"), receipt.get("files"))
    mac = commitment.get("mac")
    if not isinstance(mac, str) or re.fullmatch(r"[0-9a-f]{64}", mac) is None:
        raise _fail("build-receipt.json has an invalid producer commitment")
    return {
        "algorithm": "hmac-sha256-v1",
        "receipt_identity": list(identity),
        "source_inputs": inputs,
        "mac": mac,
    }


def _producer_commitment_core(receipt: dict[str, object], commitment: dict[str, object]) -> dict[str, object]:
    """The cross-component, versioned HMAC payload; never include its MAC."""
    core: dict[str, object] = {
        "files": receipt["files"],
        "https_port": receipt["https_port"],
        "lan_ip": receipt["lan_ip"],
        "ok": True,
        "receipt_identity": commitment["receipt_identity"],
        "runtime_publication": receipt["runtime_publication"],
        "schema": receipt["schema"],
        "source_inputs": commitment["source_inputs"],
    }
    if "tls_publication" in receipt:
        core["tls_publication"] = receipt["tls_publication"]
    return core


def _producer_commitment_mac(key: bytes, receipt: dict[str, object], commitment: dict[str, object]) -> str:
    payload = json.dumps(
        _producer_commitment_core(receipt, commitment),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, _RUNTIME_RECEIPT_DOMAIN + payload, hashlib.sha256).hexdigest()


def _source_input_snapshot(
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
) -> dict[str, dict[str, object]]:
    if set(source_postimages) != _RECEIPT_SOURCE_PATHS:
        raise _fail("invalid runtime receipt source snapshot")
    return {
        path: {"identity": list(source_postimages[path][0]), "sha256": source_postimages[path][1]}
        for path in sorted(_RECEIPT_SOURCE_PATHS)
    }


def _verify_producer_commitment(
    receipt: dict[str, object],
    receipt_identity: tuple[int, int, int, int, int, int, int],
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    key: bytes,
    *,
    allow_stale_served_tls: bool = False,
) -> None:
    commitment = _producer_commitment(receipt)
    immutable = _receipt_immutable_identity(commitment["receipt_identity"])
    if receipt_identity[:4] != immutable or receipt_identity[2] != 0o644 or receipt_identity[3] != 1:
        raise _fail("build-receipt.json does not match its producer receipt identity")
    observed_inputs = _source_input_snapshot(source_postimages)
    if commitment["source_inputs"] != observed_inputs:
        dynamic = {"tls/server-chain.crt", "tls/server.crt"}
        if (
            not allow_stale_served_tls
            or any(
                commitment["source_inputs"][path] != observed_inputs[path]
                for path in _RECEIPT_SOURCE_PATHS - dynamic
                - {"tls/ca.crt"}
            )
            # A descriptor-rooted generation exchange copies the unchanged
            # CA into a new private directory, so its inode is expected to
            # differ across A/B.  Its bytes are trust material and must stay
            # exactly the same while the retained A receipt is captured.
            or commitment["source_inputs"]["tls/ca.crt"]["sha256"]
            != observed_inputs["tls/ca.crt"]["sha256"]
        ):
            raise _fail("build-receipt.json does not match its producer source inputs")
    expected = _producer_commitment_mac(key, receipt, commitment)
    mac = commitment["mac"]
    assert isinstance(mac, str)
    if not hmac.compare_digest(mac, expected):
        raise _fail("build-receipt.json producer commitment authentication failed")


def _receipt_file_contract(
    receipt: object,
    *,
    allow_unsigned: bool = False,
) -> dict[str, object]:
    """Validate a completed v2 receipt; unsigned cores are never consumers."""
    required_files = {
        "bin/termdeck-web-server",
        "nginx/nginx.conf",
        "tls/ca.crt",
        "tls/server-chain.crt",
        "tls/server.crt",
    }
    if not isinstance(receipt, dict):
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt")
    schema = receipt.get("schema")
    core = {
        "files", "https_port", "lan_ip", "ok", "runtime_publication",
        "schema", "tls_publication",
    }
    allowed_top_levels = [
        core | {"producer_attestation"},
        # Read-only upgrade adapter for the last HMAC-producing release.
        core | {"producer_commitment"},
    ]
    if allow_unsigned:
        allowed_top_levels.append(core)
    if (
        schema != _RUNTIME_RECEIPT_SCHEMA
        or not any(set(receipt) == allowed for allowed in allowed_top_levels)
        or receipt.get("ok") is not True
        or not isinstance(receipt.get("https_port"), int)
        or isinstance(receipt.get("https_port"), bool)
        or not 1 <= receipt["https_port"] <= 65535
        or not isinstance(receipt.get("lan_ip"), str)
        or not isinstance(receipt.get("files"), dict)
        or set(receipt["files"]) != required_files
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in receipt["files"].values())
    ):
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt")
    try:
        address = ipaddress.ip_address(receipt["lan_ip"])
    except ValueError as exc:
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
    private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if address.version != 4 or not any(address in network for network in private):
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt")
    _tls_publication_contract(receipt["tls_publication"])
    if "producer_commitment" in receipt:
        _producer_commitment(receipt)
    elif "producer_attestation" in receipt:
        if not isinstance(receipt.get("producer_attestation"), dict):
            raise _fail("build-receipt.json has an invalid producer attestation")
    elif not allow_unsigned:
        raise _fail("build-receipt.json lacks a producer attestation")
    return receipt


def _publication_identity(value: object, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 7
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[3] != 1
    ):
        raise _fail(f"build-receipt.json has invalid publication identity for {label}")
    return tuple(value)  # type: ignore[return-value]


def _tls_directory_identity(value: object, *, label: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
        or value[1] <= 0
        or value[2] != 0o700
    ):
        raise _fail(f"build-receipt.json has invalid {label}")
    return tuple(value)  # type: ignore[return-value]


def _tls_manifest_contract(value: object, *, label: str) -> dict[str, dict[str, object]]:
    required_modes = {"server.key": 0o600, "server.crt": 0o644, "server-chain.crt": 0o644, "server.ext": 0o600}
    required = {"ca.crt", "ca.key", *required_modes}
    allowed = required | {"ca.srl"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise _fail(f"build-receipt.json has invalid {label}")
    parsed: dict[str, dict[str, object]] = {}
    for name in sorted(value):
        entry = value[name]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "sha256"}:
            raise _fail(f"build-receipt.json has invalid {label}")
        identity = _publication_identity(entry["identity"], label=f"{label}/{name}")
        mode = entry["mode"]
        digest = entry["sha256"]
        if (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode != identity[2]
            or (name in required_modes and mode != required_modes[name])
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise _fail(f"build-receipt.json has invalid {label}")
        parsed[name] = {"identity": list(identity), "mode": mode, "sha256": digest}
    return parsed


def _tls_publication_contract(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"generation", "current", "prior"}:
        raise _fail("build-receipt.json has invalid TLS publication binding")
    generation = value["generation"]
    current = value["current"]
    prior = value["prior"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise _fail("build-receipt.json has invalid TLS publication generation")
    if not isinstance(current, dict) or set(current) != {"path", "identity", "manifest"} or current.get("path") != "tls":
        raise _fail("build-receipt.json has invalid current TLS publication")
    normalized: dict[str, object] = {
        "generation": generation,
        "current": {
            "path": "tls",
            "identity": list(_tls_directory_identity(current.get("identity"), label="current TLS identity")),
            "manifest": _tls_manifest_contract(current.get("manifest"), label="current TLS manifest"),
        },
        "prior": None,
    }
    if prior is None:
        return normalized
    if not isinstance(prior, dict) or set(prior) != {"journal", "journal_identity", "path", "identity", "manifest"}:
        raise _fail("build-receipt.json has invalid prior TLS publication")
    journal = prior.get("journal")
    if not isinstance(journal, str) or journal != ".termdeck-tls-transaction-" + generation or prior.get("path") != "prior-tls":
        raise _fail("build-receipt.json has invalid prior TLS publication")
    normalized["prior"] = {
        "journal": journal,
        "journal_identity": list(_tls_directory_identity(prior.get("journal_identity"), label="TLS journal identity")),
        "path": "prior-tls",
        "identity": list(_tls_directory_identity(prior.get("identity"), label="prior TLS identity")),
        "manifest": _tls_manifest_contract(prior.get("manifest"), label="prior TLS manifest"),
    }
    return normalized


def _runtime_output_expectations(
    receipt: dict[str, object],
) -> dict[str, tuple[tuple[int, int, int, int, int, int, int], int, str]]:
    """Parse the two exact v2 leaves that an A receipt owns."""
    if receipt.get("schema") != _RUNTIME_RECEIPT_SCHEMA:
        raise _fail("build-receipt.json lacks the v2 runtime-publication binding")
    publication = receipt.get("runtime_publication")
    if not isinstance(publication, dict) or set(publication) != {"generation", "outputs"}:
        raise _fail("build-receipt.json has invalid runtime-publication binding")
    generation = publication["generation"]
    outputs = publication["outputs"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise _fail("build-receipt.json has invalid runtime-publication generation")
    expected_modes = {
        "current-lan-ip": 0o600,
        "nginx/allowed-hosts.map": 0o600,
    }
    if not isinstance(outputs, dict) or set(outputs) != set(expected_modes):
        raise _fail("build-receipt.json has invalid runtime-publication outputs")
    result: dict[str, tuple[tuple[int, int, int, int, int, int, int], int, str]] = {}
    for path, mode in expected_modes.items():
        entry = outputs[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "path", "sha256"}:
            raise _fail(f"build-receipt.json has invalid postimage entry for {path}")
        identity = _publication_identity(entry["identity"], label=path)
        digest = entry["sha256"]
        if (
            entry["path"] != path
            or entry["mode"] != mode
            or identity[2] != mode
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise _fail(f"build-receipt.json has invalid postimage entry for {path}")
        result[path] = (identity, mode, digest)
    return result


def _assert_tls_journal_current(
    runtime: Anchor,
    name: str,
    descriptor: int,
    expected: tuple[int, int, int, int],
    *,
    anchors: tuple[Anchor, ...],
) -> None:
    _assert_all(anchors, phase="before using TLS prior-generation journal")
    try:
        opened = _directory_identity(os.fstat(descriptor), label="TLS prior-generation journal")
    except OSError as exc:
        raise _fail("TLS prior-generation journal descriptor is unavailable", exc) from exc
    named = _lstat_at(runtime.descriptor, name, label="TLS prior-generation journal")
    if (
        opened != expected
        or named is None
        or stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _directory_identity(named, label="TLS prior-generation journal") != expected
        or expected[2] != 0o700
        or expected[3] != runtime.chain[-1][3]
    ):
        raise _fail("TLS prior-generation journal changed")


def _open_tls_prior_journal(
    runtime: Anchor,
    publication: dict[str, object],
    *,
    anchors: tuple[Anchor, ...],
) -> tuple[int, str, tuple[int, int, int, int]] | None:
    prior = publication["prior"]
    if prior is None:
        return None
    assert isinstance(prior, dict)
    name = prior["journal"]
    identity = _tls_directory_identity(prior["journal_identity"], label="TLS journal identity")
    assert isinstance(name, str)
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=runtime.descriptor)
    except OSError as exc:
        raise _fail("could not securely open TLS prior-generation journal", exc) from exc
    try:
        _assert_tls_journal_current(runtime, name, descriptor, identity, anchors=anchors)
        return descriptor, name, identity
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_from_directory(
    descriptor: int,
    name: str,
    *,
    label: str,
    expected: tuple[int, int, int, int, int, int, int] | None = None,
) -> tuple[bytes, tuple[int, int, int, int, int, int, int]]:
    before = _trusted_leaf(descriptor, name, label=label)
    if before is None:
        raise _fail(f"missing {label}")
    if expected is not None and not _same_after_rename(before, expected):
        raise _fail(f"{label} does not match its committed identity")
    try:
        opened_descriptor = os.open(name, _READ_FLAGS, dir_fd=descriptor)
    except OSError as exc:
        raise _fail(f"could not securely open {label}", exc) from exc
    try:
        opened = _file_identity(os.fstat(opened_descriptor), label=label)
        if not _same_object(opened, before):
            raise _fail(f"{label} changed while opening")
        chunks: list[bytes] = []
        while True:
            block = os.read(opened_descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = _file_identity(os.fstat(opened_descriptor), label=label)
        named_after = _trusted_leaf(descriptor, name, label=label)
        if after != opened or named_after is None or not _same_object(named_after, before):
            raise _fail(f"{label} changed while reading")
        return b"".join(chunks), before
    finally:
        os.close(opened_descriptor)


def _restore_held_journal_foreign_regular(
    source: Anchor,
    source_name: str,
    journal_descriptor: int,
    journal_name: str,
    journal_identity: tuple[int, int, int, int],
    retained_name: str,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
) -> bool:
    """Re-link a foreign leaf moved into a TLS journal only from its held FD.

    A replacement can win after the caller validates its source but before
    renameat2 moves the name.  The moved object is then foreign evidence, not
    a prior-generation leaf.  Restore it only if it is still a singly linked
    regular file and the public name remains empty; linking `/proc/self/fd/N`
    avoids consulting the mutable journal name after the FD is held.
    """
    _assert_all(anchors, phase=f"before restoring moved foreign {label}")
    _assert_tls_journal_current(
        anchors[0], journal_name, journal_descriptor, journal_identity, anchors=anchors
    )
    if _lstat_at(source.descriptor, source_name, label=label) is not None:
        return False
    info = _lstat_at(journal_descriptor, retained_name, label=f"TLS journal {retained_name}")
    if (
        info is None
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        return False
    try:
        descriptor = os.open(retained_name, _READ_FLAGS, dir_fd=journal_descriptor)
    except OSError as exc:
        raise _fail(f"could not securely hold moved foreign {label}", exc) from exc
    try:
        held = _file_identity(os.fstat(descriptor), label=f"moved foreign {label}")
        named = _lstat_at(journal_descriptor, retained_name, label=f"TLS journal {retained_name}")
        if (
            named is None
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or (int(named.st_dev), int(named.st_ino), stat.S_IMODE(named.st_mode))
            != (held[0], held[1], held[2])
        ):
            return False
        _assert_all(anchors, phase=f"immediately before restoring moved foreign {label}")
        _assert_tls_journal_current(
            anchors[0], journal_name, journal_descriptor, journal_identity, anchors=anchors
        )
        try:
            os.link(
                f"/proc/self/fd/{descriptor}",
                source_name,
                dst_dir_fd=source.descriptor,
                follow_symlinks=True,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise _fail(f"could not restore moved foreign {label}", exc) from exc
        restored = _lstat_at(source.descriptor, source_name, label=label)
        if (
            restored is None
            or stat.S_ISLNK(restored.st_mode)
            or not stat.S_ISREG(restored.st_mode)
            or (int(restored.st_dev), int(restored.st_ino), stat.S_IMODE(restored.st_mode))
            != (held[0], held[1], held[2])
        ):
            raise _fail(f"moved foreign {label} changed during restoration")
        _fsync(journal_descriptor, label=f"TLS journal after restoring moved foreign {label}")
        _fsync(source.descriptor, label=f"parent after restoring moved foreign {label}")
        _assert_all(anchors, phase=f"after restoring moved foreign {label}")
        return True
    finally:
        os.close(descriptor)


def _capture_prior_runtime_leaf(
    source: Anchor,
    source_name: str,
    journal_descriptor: int,
    journal_name: str,
    journal_identity: tuple[int, int, int, int],
    destination_name: str,
    expected: tuple[int, int, int, int, int, int, int],
    digest: str,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
) -> None:
    _assert_tls_journal_current(source if source.label == "TermDeck runtime parent" else anchors[0], journal_name, journal_descriptor, journal_identity, anchors=anchors)
    retained = _trusted_leaf(journal_descriptor, destination_name, label=f"TLS journal {destination_name}")
    if retained is not None:
        data, observed = _read_regular_from_directory(
            journal_descriptor,
            destination_name,
            label=f"TLS journal {destination_name}",
            expected=expected,
        )
        if observed is None or hashlib.sha256(data).hexdigest() != digest:
            raise _fail(f"TLS journal {destination_name} changed")
        return
    observed = _trusted_leaf(source.descriptor, source_name, label=label)
    if observed != expected:
        raise _fail(f"{label} changed before prior-generation capture")
    data = _read_regular(source, source_name, label=label, anchors=anchors)
    if hashlib.sha256(data).hexdigest() != digest:
        raise _fail(f"{label} changed before prior-generation capture")
    _assert_all(anchors, phase=f"immediately before capturing prior {label}")
    _assert_tls_journal_current(anchors[0], journal_name, journal_descriptor, journal_identity, anchors=anchors)
    try:
        _renameat2(
            source.descriptor,
            source_name,
            journal_descriptor,
            destination_name,
            label=f"capture prior {label}",
        )
    except (FileExistsError, FileNotFoundError) as exc:
        raise _fail(f"{label} changed before prior-generation capture", exc) from exc
    moved = _trusted_leaf(journal_descriptor, destination_name, label=f"TLS journal {destination_name}")
    if moved is None or not _same_after_rename(moved, expected):
        restored = _restore_held_journal_foreign_regular(
            source,
            source_name,
            journal_descriptor,
            journal_name,
            journal_identity,
            destination_name,
            label=label,
            anchors=anchors,
        )
        if restored:
            raise _fail(f"{label} changed during prior-generation capture; restored moved foreign leaf")
        raise _fail(f"{label} changed during prior-generation capture; retained ambiguous foreign leaf")
    moved_data, _ = _read_regular_from_directory(
        journal_descriptor,
        destination_name,
        label=f"TLS journal {destination_name}",
        expected=expected,
    )
    if hashlib.sha256(moved_data).hexdigest() != digest:
        restored = _restore_held_journal_foreign_regular(
            source,
            source_name,
            journal_descriptor,
            journal_name,
            journal_identity,
            destination_name,
            label=label,
            anchors=anchors,
        )
        if restored:
            raise _fail(f"{label} changed during prior-generation capture; restored moved foreign leaf")
        raise _fail(f"{label} changed during prior-generation capture; retained ambiguous foreign leaf")
    _fsync(journal_descriptor, label=f"TLS journal after capturing prior {label}")
    _fsync(source.descriptor, label=f"parent after capturing prior {label}")
    _assert_all(anchors, phase=f"after capturing prior {label}")


def _capture_prior_runtime_generation(
    publication: dict[str, object],
    runtime: Anchor,
    nginx: Anchor,
    source_postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    control_key: bytes,
    *,
    anchors: tuple[Anchor, ...],
) -> bool:
    """Retain exact A output inodes beside the A TLS generation before B writes.

    The preceding TLS exchange has already made A's receipt non-public.  A
    retry can resume a partially completed capture by re-proving every leaf
    against that retained, authenticated A receipt; it never reconstructs a
    receipt or output from bytes.
    """
    opened = _open_tls_prior_journal(runtime, publication, anchors=anchors)
    if opened is None:
        return False
    journal_descriptor, journal_name, journal_identity = opened
    try:
        raw, receipt_identity = _read_regular_from_directory(
            journal_descriptor,
            _PRIOR_RECEIPT,
            label=f"TLS journal {_PRIOR_RECEIPT}",
        )
        try:
            receipt = _receipt_file_contract(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("TLS journal prior build receipt is invalid", exc) from exc
        if "producer_attestation" in receipt:
            _run_attestation_helper(
                runtime.path + "/" + journal_name + "/" + _PRIOR_RECEIPT,
                label="TLS journal prior build receipt",
            )
            attested_identity = receipt["producer_attestation"].get("receipt_core_identity")
            if (
                not isinstance(attested_identity, list)
                or len(attested_identity) != 7
                or tuple(attested_identity[:4]) != receipt_identity[:4]
            ):
                raise _fail("TLS journal prior build receipt changed from its attested inode")
        else:
            _verify_producer_commitment(
                receipt,
                receipt_identity,
                source_postimages,
                control_key,
                allow_stale_served_tls=True,
            )
        receipt_publication = _tls_publication_contract(receipt.get("tls_publication"))
        prior = publication["prior"]
        assert isinstance(prior, dict)
        receipt_current = receipt_publication["current"]
        assert isinstance(receipt_current, dict)
        # A rotation may have been forced precisely because served A leaves
        # drifted (for example a key/certificate mismatch).  The A receipt
        # still owns its map/current leaves if its private TLS directory is
        # the same generation; recovery later validates the retained A
        # inventory independently before it can ever be restored.
        if receipt_current["identity"] != prior["identity"]:
            raise _fail("TLS journal prior build receipt does not bind the retained A TLS directory")
        expected = _runtime_output_expectations(receipt)
        current_identity, _current_mode, current_digest = expected["current-lan-ip"]
        map_identity, _map_mode, map_digest = expected["nginx/allowed-hosts.map"]
        _capture_prior_runtime_leaf(
            runtime,
            "current-lan-ip",
            journal_descriptor,
            journal_name,
            journal_identity,
            _PRIOR_CURRENT,
            current_identity,
            current_digest,
            label="current-lan-ip",
            anchors=anchors,
        )
        _capture_prior_runtime_leaf(
            nginx,
            "allowed-hosts.map",
            journal_descriptor,
            journal_name,
            journal_identity,
            _PRIOR_HOSTS,
            map_identity,
            map_digest,
            label="nginx/allowed-hosts.map",
            anchors=anchors,
        )
        return True
    finally:
        os.close(journal_descriptor)


def _tls_manifest_snapshot(
    postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
) -> dict[str, dict[str, object]]:
    return {
        name: {"identity": list(identity), "mode": identity[2], "sha256": digest}
        for name, (identity, digest) in sorted(postimages.items())
    }


def _verify_tls_publication_current(
    publication: dict[str, object],
    tls: Anchor,
    postimages: dict[str, tuple[tuple[int, int, int, int, int, int, int], str]],
    *,
    anchors: tuple[Anchor, ...],
) -> None:
    _assert_all(anchors, phase="before validating TLS receipt provenance")
    current = publication["current"]
    assert isinstance(current, dict)
    if tuple(current["identity"]) != tls.chain[-1]:
        raise _fail("TLS receipt provenance no longer owns the current TLS directory")
    if current["manifest"] != _tls_manifest_snapshot(postimages):
        raise _fail("TLS receipt provenance does not match current TLS postimages")
    _assert_all(anchors, phase="after validating TLS receipt provenance")


def _verify_runtime_publication_receipt(
    receipt: object,
    runtime: Anchor,
    nginx: Anchor,
    *,
    anchors: tuple[Anchor, ...],
    allow_unsigned: bool = False,
) -> None:
    """Require v2 postimages to still be the exact transaction-owned leaves."""
    parsed = _receipt_file_contract(receipt, allow_unsigned=allow_unsigned)
    if parsed["schema"] != "termdeck.lan-runtime-build.v2":
        raise _fail("build-receipt.json lacks the v2 runtime-publication binding")
    publication = parsed["runtime_publication"]
    if not isinstance(publication, dict) or set(publication) != {"generation", "outputs"}:
        raise _fail("build-receipt.json has invalid runtime-publication binding")
    generation = publication["generation"]
    outputs = publication["outputs"]
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise _fail("build-receipt.json has invalid runtime-publication generation")
    expected_outputs = {
        "current-lan-ip": (runtime, "current-lan-ip", 0o600),
        "nginx/allowed-hosts.map": (nginx, "allowed-hosts.map", 0o600),
    }
    if not isinstance(outputs, dict) or set(outputs) != set(expected_outputs):
        raise _fail("build-receipt.json has invalid runtime-publication outputs")
    _assert_all(anchors, phase="before validating receipt postimages")
    for path, (parent, name, mode) in expected_outputs.items():
        entry = outputs[path]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "path", "sha256"}:
            raise _fail(f"build-receipt.json has invalid postimage entry for {path}")
        identity = _publication_identity(entry["identity"], label=path)
        if (
            entry["path"] != path
            or entry["mode"] != mode
            or identity[2] != mode
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise _fail(f"build-receipt.json has invalid postimage entry for {path}")
        observed = _trusted_leaf(parent.descriptor, name, label=path)
        if observed is None or observed != identity:
            raise _fail(f"runtime publication postimage changed for {path}")
        content = _read_regular(parent, name, label=path, anchors=anchors)
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise _fail(f"runtime publication postimage changed for {path}")
    _assert_all(anchors, phase="after validating receipt postimages")


def _require_managed_receipt_if_present(
    runtime: Anchor,
    nginx: Anchor,
    sources: tuple[tuple[str, Anchor, str], ...],
    control_key: bytes,
    *,
    anchors: tuple[Anchor, ...],
) -> tuple[
    tuple[int, int, int, int, int, int, int],
    dict[str, tuple[int, int, int, int, int, int, int] | None],
] | None:
    """Reject a foreign or drifted receipt before its public name can move."""
    label = "build-receipt.json"
    expected = _trusted_leaf(runtime.descriptor, label, label=label)
    if expected is None:
        return None
    raw = _read_regular(runtime, label, label=label, anchors=anchors)
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
    parsed = _receipt_file_contract(receipt)
    source_postimages = _capture_source_postimages(sources, anchors=anchors)
    _verify_runtime_publication_receipt(parsed, runtime, nginx, anchors=anchors)
    if "producer_attestation" in parsed:
        _run_attestation_helper(
            runtime.path + "/" + label,
            label=label,
        )
        attested_identity = parsed["producer_attestation"].get("receipt_core_identity")
        if (
            not isinstance(attested_identity, list)
            or len(attested_identity) != 7
            or tuple(attested_identity[:4]) != expected[:4]
        ):
            raise _fail("build-receipt.json changed from its attested inode")
    else:
        # A fully authenticated legacy HMAC receipt is accepted once as A.
        # Its signed source manifest describes history and may differ from the
        # newly installed B executable/config; this producer never emits HMAC.
        commitment = _producer_commitment(parsed)
        if expected[:4] != _receipt_immutable_identity(commitment["receipt_identity"]):
            raise _fail("build-receipt.json does not match its producer receipt identity")
        mac = commitment["mac"]
        assert isinstance(mac, str)
        if not hmac.compare_digest(mac, _producer_commitment_mac(control_key, parsed, commitment)):
            raise _fail("build-receipt.json producer commitment authentication failed")
    outputs = {
        path: identity
        for path, (identity, _mode, _digest) in _runtime_output_expectations(parsed).items()
    }
    _assert_all(anchors, phase="after validating existing build receipt")
    observed = _trusted_leaf(runtime.descriptor, label, label=label)
    if observed != expected:
        raise _fail("build-receipt.json changed while validating its managed identity")
    return expected, outputs


def _main() -> None:
    if len(sys.argv) != 5:
        raise _fail("invalid runtime publication invocation")
    runtime_path = sys.argv[1]
    lan_ip_object = ipaddress.ip_address(sys.argv[2])
    preflight_guards = sys.argv[3]
    serialized_tls_publication = sys.argv[4]
    lan_ip = str(lan_ip_object)
    private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if lan_ip_object.version != 4 or not any(lan_ip_object in network for network in private):
        raise _fail(f"TermDeck receipt refused non-private IPv4 address: {lan_ip}")

    runtime = _open_anchor(runtime_path, label="TermDeck runtime parent")
    nginx = _open_anchor(runtime_path + "/nginx", label="TermDeck nginx runtime parent")
    tls = _open_anchor(runtime_path + "/tls", label="TermDeck TLS runtime parent")
    binaries = _open_anchor(runtime_path + "/bin", label="TermDeck binary runtime parent")
    control_parent = _open_anchor(os.path.dirname(runtime_path), label="TermDeck runtime receipt control parent")
    anchors = (runtime, nginx, tls, binaries)
    try:
        _bind_preflight_guards(preflight_guards, runtime_path, anchors)
        control_key = _load_producer_commitment_key(control_parent, runtime, anchors=anchors)
        config = _read_regular(nginx, "nginx.conf", label="nginx/nginx.conf", anchors=anchors)
        try:
            config_text = config.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("TermDeck receipt could not decode nginx/nginx.conf", exc) from exc
        listen = re.findall(r"^\s*listen\s+[^;:]+:(\d+)\s+ssl\s*;\s*$", config_text, re.MULTILINE)
        if len(listen) != 1:
            raise _fail("TermDeck receipt requires exactly one TLS listen port")
        https_port = int(listen[0])
        if not 1 <= https_port <= 65535:
            raise _fail(f"TermDeck receipt found invalid HTTPS port: {https_port}")

        sources = (
            ("bin/termdeck-web-server", binaries, "termdeck-web-server"),
            ("nginx/nginx.conf", nginx, "nginx.conf"),
            ("tls/ca.crt", tls, "ca.crt"),
            ("tls/server-chain.crt", tls, "server-chain.crt"),
            ("tls/server.crt", tls, "server.crt"),
        )
        # Validate the complete served TLS contract inside this final helper,
        # rather than trusting the preceding process's validation.  Keep the
        # private-key digest only in this transient descriptor-rooted snapshot.
        tls_postimages = _validate_tls_postimages(tls, lan_ip, anchors=anchors)
        try:
            tls_publication = _tls_publication_contract(json.loads(serialized_tls_publication))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("TermDeck receipt received invalid TLS publication provenance", exc) from exc
        _verify_tls_publication_current(tls_publication, tls, tls_postimages, anchors=anchors)
        source_postimages = _capture_source_postimages(sources, anchors=anchors)
        files = {
            relative: digest
            for relative, (_identity, digest) in source_postimages.items()
        }
        # A completed TLS rotation already has A's receipt in its signed
        # journal.  Retain the receipt-owned A map/current inodes there before
        # this helper can write any B output, so an activation rollback never
        # has to recreate receipt-bound leaves from bytes.
        _capture_prior_runtime_generation(
            tls_publication,
            runtime,
            nginx,
            source_postimages,
            control_key,
            anchors=anchors,
        )
        allowed_hosts = (
            "localhost 1;\n"
            "127.0.0.1 1;\n"
            "termdeck.local 1;\n"
            "max.local 1;\n"
            f"{lan_ip} 1;\n"
            "maxchat.semiotech.org 1;\n"
            "semiotech.org 1;\n"
            "termdeck.semiotech.org 1;\n"
        ).encode("ascii")
        # An existing receipt describes the old map/current pair.  Move it to
        # a durable private tombstone before either file changes; a later
        # failure can therefore never leave a stale public receipt behind.
        verified_previous = _require_managed_receipt_if_present(
            runtime,
            nginx,
            sources,
            control_key,
            anchors=anchors,
        )
        verified_previous_receipt = verified_previous[0] if verified_previous is not None else None
        expected_outputs = verified_previous[1] if verified_previous is not None else {}
        previous_receipt = _take_trusted_prior(
            runtime,
            "build-receipt.json",
            label="build-receipt.json",
            anchors=anchors,
            expected=verified_previous_receipt,
            require_absent=verified_previous_receipt is None,
        )
        receipt_publication = ReceiptPublication()
        try:
            # Bind the exact descriptor-observed postimages into a fresh
            # generation before publishing the receipt.  The receipt is the
            # final public mutation, so it never validates a partly updated
            # map/current pair.
            generation = secrets.token_hex(16)
            current_lan_ip = (lan_ip + "\n").encode("ascii")
            map_identity = _publish(
                nginx,
                "allowed-hosts.map",
                allowed_hosts,
                0o600,
                label="nginx/allowed-hosts.map",
                anchors=anchors,
                require_absent=verified_previous_receipt is None,
                expected=expected_outputs.get("nginx/allowed-hosts.map"),
            )
            current_identity = _publish(
                runtime,
                "current-lan-ip",
                current_lan_ip,
                0o600,
                label="current-lan-ip",
                anchors=anchors,
                require_absent=verified_previous_receipt is None,
                expected=expected_outputs.get("current-lan-ip"),
            )
            _verify_source_postimages(sources, source_postimages, anchors=anchors)
            _verify_tls_postimages(tls, lan_ip, tls_postimages, anchors=anchors)
            receipt_core = {
                "files": files,
                "https_port": https_port,
                "lan_ip": lan_ip,
                "ok": True,
                "runtime_publication": {
                    "generation": generation,
                    "outputs": {
                        "current-lan-ip": {
                            "identity": list(current_identity),
                            "mode": 0o600,
                            "path": "current-lan-ip",
                            "sha256": hashlib.sha256(current_lan_ip).hexdigest(),
                        },
                        "nginx/allowed-hosts.map": {
                            "identity": list(map_identity),
                            "mode": 0o600,
                            "path": "nginx/allowed-hosts.map",
                            "sha256": hashlib.sha256(allowed_hosts).hexdigest(),
                        },
                    },
                },
                "tls_publication": tls_publication,
                "schema": _RUNTIME_RECEIPT_SCHEMA,
            }
            _verify_runtime_publication_receipt(
                receipt_core,
                runtime,
                nginx,
                anchors=anchors,
                allow_unsigned=True,
            )
            receipt_payload = (
                json.dumps(
                    receipt_core,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")

            def verify_final_postimages() -> None:
                _verify_source_postimages(sources, source_postimages, anchors=anchors)
                _verify_tls_postimages(tls, lan_ip, tls_postimages, anchors=anchors)

            receipt_identity = _publish(
                runtime,
                "build-receipt.json",
                receipt_payload,
                0o644,
                label="build-receipt.json",
                anchors=anchors,
                before_publication=verify_final_postimages,
                require_absent=True,
                receipt_publication=receipt_publication,
            )
            # The unsigned canonical core is now the public build receipt.
            # The fixed helper opens that exact path O_RDWR, sends the same FD
            # to the root Ed25519 signer, verifies its response, and rewrites
            # this inode in place with producer_attestation.
            _run_attestation_helper(
                runtime.path + "/build-receipt.json",
                transaction_id=generation,
                label="build-receipt.json",
            )
            finalized_identity = receipt_publication.rebind_finalized(
                runtime,
                anchors=anchors,
            )
            receipt_raw = _read_regular(runtime, "build-receipt.json", label="build-receipt.json", anchors=anchors)
            try:
                receipt = _receipt_file_contract(json.loads(receipt_raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _fail("build-receipt.json became invalid during publication", exc) from exc
            observed_receipt = _trusted_leaf(runtime.descriptor, "build-receipt.json", label="build-receipt.json")
            if (
                observed_receipt != finalized_identity
                or finalized_identity[:4] != receipt_identity[:4]
                or "producer_attestation" not in receipt
                or "producer_commitment" in receipt
            ):
                raise _fail("build-receipt.json changed during attested publication")
            attested_identity = receipt["producer_attestation"].get("receipt_core_identity")
            if (
                not isinstance(attested_identity, list)
                or len(attested_identity) != 7
                or tuple(attested_identity) != receipt_identity
            ):
                raise _fail("build-receipt.json attestation does not bind its unsigned core")
            _run_attestation_helper(
                runtime.path + "/build-receipt.json",
                label="build-receipt.json",
            )
            _verify_runtime_publication_receipt(receipt, runtime, nginx, anchors=anchors)
            try:
                verify_final_postimages()
            except PublicationError as source_exc:
                raise _fail("TermDeck receipt sources changed during final publication", source_exc) from source_exc
            _verify_runtime_publication_receipt(receipt, runtime, nginx, anchors=anchors)
            observed_receipt = _trusted_leaf(runtime.descriptor, "build-receipt.json", label="build-receipt.json")
            if observed_receipt != finalized_identity:
                # The parsed bytes above are no longer authoritative when a
                # same-inode writer changes mtime/size/content between proofs.
                # Retain the public evidence rather than tombstoning an object
                # we cannot prove is our just-published receipt.
                raise _fail("build-receipt.json changed during final verification")
            _run_attestation_helper(
                runtime.path + "/build-receipt.json",
                label="build-receipt.json",
            )
            try:
                verify_final_postimages()
            except PublicationError as source_exc:
                raise _fail("TermDeck receipt sources changed during final verification", source_exc) from source_exc
            if previous_receipt is not None:
                _retain_trusted_prior(previous_receipt[0], previous_receipt[1], anchors=anchors)
        except BaseException as exc:
            # Cover the rename itself, every verification step, and private
            # retention.  Ownership is held before rename so an interruption
            # cannot bypass cleanup merely by preventing _publish's return.
            # A changed receipt remains foreign evidence at its public name.
            try:
                receipt_publication.invalidate(runtime, anchors=anchors)
            except BaseException as cleanup_exc:
                exc.add_note(f"could not invalidate owned build receipt: {cleanup_exc}")
            raise
        finally:
            receipt_publication.close()
            if previous_receipt is not None:
                previous_receipt[0].close()
    finally:
        for anchor in reversed(anchors):
            anchor.close()
        control_parent.close()


try:
    _main()
except PublicationError as exc:
    sys.stderr.write(f"TermDeck runtime publication failed: {exc}\n")
    raise SystemExit(1)
PY
