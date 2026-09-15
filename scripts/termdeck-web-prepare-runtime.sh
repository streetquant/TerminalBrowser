#!/usr/bin/bash
set -euo pipefail
umask 077

runtime=/warp/runtime/termdeck/termdeck-lan
tls="$runtime/tls"
nginx_root="$runtime/nginx"
bin="$runtime/bin"
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


def prepare(path: str, *, exact_mode: bool, create: bool) -> tuple[tuple[int, int, int, int], ...]:
    descriptor = os.open("/", flags)
    chain = [directory_identity(os.fstat(descriptor), path="/")]
    try:
        for item in components(path):
            try:
                child = os.open(item, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError(f"required directory is missing: {path}")
                os.mkdir(item, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(item, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            chain.append(directory_identity(os.fstat(descriptor), path=path))
        if exact_mode:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            chain[-1] = directory_identity(os.fstat(descriptor), path=path)
        return tuple(chain)
    finally:
        os.close(descriptor)


try:
    requested = sys.argv[1:]
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
PY
)

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

ca_cert="$tls/ca.crt"
ca_key="$tls/ca.key"
cert="$tls/server.crt"
key="$tls/server.key"
chain="$tls/server-chain.crt"
for required in "$ca_cert" "$ca_key"; do
    [ -s "$required" ] || { echo "missing TermDeck LAN CA material: $required" >&2; exit 1; }
done

rotate=0
if [ ! -s "$cert" ] || [ ! -s "$key" ] || [ ! -s "$chain" ]; then
    rotate=1
elif ! /usr/bin/openssl x509 -in "$cert" -noout -checkend 2592000 >/dev/null 2>&1; then
    rotate=1
elif ! /usr/bin/openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | /usr/bin/grep -Fq "IP Address:$lan_ip"; then
    rotate=1
fi
if [ "$rotate" -eq 1 ]; then
    verify_runtime_guards
    work=$(/usr/bin/mktemp -d "$tls/.rotate.XXXXXX")
    # Keep the private rotation directory as recovery evidence.  A pathname
    # based recursive cleanup would have a final check/delete race: a
    # same-UID writer could replace "$work" after a parent guard and make an
    # otherwise successful rotation recursively delete a foreign directory.
    # The directory is mktemp-created beneath the descriptor-guarded 0700 TLS
    # parent and contains only rotation inputs/outputs, so safe retention is
    # preferable to a broad deletion on either success or failure.
    cat >"$work/server.ext" <<EOF_EXT
[v3_req]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=@alt_names
[alt_names]
DNS.1=termdeck.local
DNS.2=max.local
IP.1=$lan_ip
IP.2=127.0.0.1
EOF_EXT
    /usr/bin/openssl req -new -newkey rsa:2048 -nodes -subj '/CN=termdeck.local' \
        -keyout "$work/server.key" -out "$work/server.csr" >/dev/null 2>&1
    serial=$(/usr/bin/openssl rand -hex 16)
    if ! [[ "$serial" =~ ^[0-9a-f]{32}$ ]]; then
        echo "TermDeck could not generate a certificate serial" >&2
        exit 1
    fi
    /usr/bin/openssl x509 -req -in "$work/server.csr" -CA "$ca_cert" -CAkey "$ca_key" \
        -set_serial "0x$serial" -days 825 -sha256 -extfile "$work/server.ext" \
        -extensions v3_req -out "$work/server.crt" >/dev/null 2>&1
    /usr/bin/openssl verify -CAfile "$ca_cert" "$work/server.crt" >/dev/null
    cat "$work/server.crt" "$ca_cert" >"$work/server-chain.crt"
    chmod 600 "$work/server.key"
    chmod 644 "$work/server.crt" "$work/server-chain.crt"
    verify_runtime_guards
    mv -f "$work/server.key" "$key"
    verify_runtime_guards
    mv -f "$work/server.crt" "$cert"
    verify_runtime_guards
    mv -f "$work/server-chain.crt" "$chain"
    verify_runtime_guards
    # rename(2) replaces a final symlink itself; unlike cp -f it does not
    # follow that symlink and overwrite an external target.
    mv -f "$work/server.ext" "$tls/server.ext"
    verify_runtime_guards
fi
/usr/bin/openssl verify -CAfile "$ca_cert" "$cert" >/dev/null
/usr/bin/openssl x509 -in "$cert" -noout -ext subjectAltName | /usr/bin/grep -Fq "IP Address:$lan_ip"

# Keep the LAN runtime receipt derived from the exact files that the service
# will use.  The receipt is written last, after TLS rotation and host-map
# generation, so an atomic update can never describe a partial runtime.  This
# helper uses only no-follow directory descriptors after the existing TLS work:
# the generated leaves cannot be redirected through a symlink, hard link, or a
# parent/leaf swap between validation and publication.
/usr/bin/python3 -I - "$runtime" "$lan_ip" "$runtime_guards" <<'PY'
from __future__ import annotations

import ctypes
import errno
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
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


def _fail(message: str, exc: BaseException | None = None) -> PublicationError:
    if exc is None:
        return PublicationError(message)
    return PublicationError(f"{message}: {exc}")


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
        if named is None or not stat.S_ISDIR(named.st_mode) or _directory_identity(named, label=f"{self.label} private stage") != self.identity or actual != self.identity:
            raise _fail(f"{self.label} private stage changed {phase}")

    def write_payload(self, content: bytes, mode: int, *, anchors: tuple[Anchor, ...]) -> None:
        _assert_mutation_ready(anchors, phase=f"immediately before creating staged {self.label}", stage=self)
        try:
            descriptor = os.open("payload", _WRITE_FLAGS, mode, dir_fd=self.descriptor)
        except OSError as exc:
            raise _fail(f"could not create private staged {self.label}", exc) from exc
        self.payload_descriptor = descriptor
        try:
            _assert_mutation_ready(anchors, phase=f"immediately before setting staged {self.label} mode", stage=self)
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(content):
                _assert_mutation_ready(anchors, phase=f"immediately before writing staged {self.label}", stage=self)
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise _fail(f"could not write private staged {self.label}")
                offset += written
            _fsync(descriptor, label=f"private staged {self.label}")
            observed = _file_identity(os.fstat(descriptor), label=f"private staged {self.label}")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or observed[2] != mode or observed[3] != 1 or observed[4] != len(content):
                raise _fail(f"private staged {self.label} changed while writing")
            named = _trusted_leaf(self.descriptor, "payload", label=f"private staged {self.label}")
            if named is None or not _same_object(named, observed):
                raise _fail(f"private staged {self.label} changed while writing")
            self.payload_identity = observed
            self.payload_digest = hashlib.sha256(content).digest()
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


def _restore_untrusted_prior(
    stage: PrivateStage,
    parent: Anchor,
    name: str,
    *,
    anchors: tuple[Anchor, ...],
) -> bool:
    """Restore only into an atomically empty public name; never overwrite it."""
    _assert_all(anchors, phase=f"before restoring displaced {stage.label}")
    stage.assert_current(phase="before restoring displaced tombstone")
    if _lstat_at(parent.descriptor, name, label=stage.label) is not None:
        return False
    _assert_mutation_ready(anchors, phase=f"immediately before restoring displaced {stage.label}", stage=stage)
    try:
        _renameat2(stage.descriptor, "prior", parent.descriptor, name, label=f"restore displaced {stage.label}")
    except (FileExistsError, FileNotFoundError):
        return False
    _fsync(stage.descriptor, label=f"private stage after restoring displaced {stage.label}")
    _fsync(parent.descriptor, label=f"publication parent after restoring displaced {stage.label}")
    _assert_all(anchors, phase=f"after restoring displaced {stage.label}")
    return True


def _take_trusted_prior(
    parent: Anchor,
    name: str,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
) -> tuple[PrivateStage, tuple[int, int, int, int, int, int, int]] | None:
    """Durably remove a trusted managed leaf before related files change.

    The caller owns the returned private stage.  It must either dispose that
    verified tombstone after the replacement succeeds or leave it in place as
    recovery evidence when a later operation fails.
    """
    _component(name, label=label)
    _assert_all(anchors, phase=f"before invalidating {label}")
    expected = _trusted_leaf(parent.descriptor, name, label=label)
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
            try:
                _restore_untrusted_prior(stage, parent, name, anchors=anchors)
            except BaseException as restore_exc:
                raise _fail(f"{label} changed before secure tombstone and could not be safely restored", restore_exc) from restore_exc
            raise _fail(f"{label} changed before secure tombstone")
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


def _publish(
    parent: Anchor,
    name: str,
    content: bytes,
    mode: int,
    *,
    label: str,
    anchors: tuple[Anchor, ...],
) -> None:
    _component(name, label=label)
    _assert_all(anchors, phase=f"before preflight for {label}")
    expected = _trusted_leaf(parent.descriptor, name, label=label)
    stage = _new_stage(parent, label=label, anchors=anchors)
    try:
        stage.write_payload(content, mode, anchors=anchors)
        staged = stage.verify_payload(anchors=anchors)
        if expected is None:
            _assert_mutation_ready(anchors, phase=f"immediately before no-replace publication for {label}", stage=stage)
            try:
                _renameat2(stage.descriptor, "payload", parent.descriptor, name, label=label)
            except FileExistsError as exc:
                raise _fail(f"{label} changed before no-replace publication", exc) from exc
            published = _trusted_leaf(parent.descriptor, name, label=label)
            if published is None or not _same_after_rename(published, staged):
                raise _fail(f"{label} changed during no-replace publication")
            stage.close_payload()
            _fsync(stage.descriptor, label=f"private stage after publishing {label}")
            _fsync(parent.descriptor, label=f"publication parent after publishing {label}")
            _assert_all(anchors, phase=f"after publishing {label}")
            stage.discard_empty(anchors=anchors)
            return

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
            try:
                _restore_untrusted_prior(stage, parent, name, anchors=anchors)
            except BaseException as restore_exc:
                raise _fail(f"{label} changed before secure tombstone and could not be safely restored", restore_exc) from restore_exc
            raise _fail(f"{label} changed before secure tombstone")
        _fsync(stage.descriptor, label=f"private stage after tombstoning {label}")
        _fsync(parent.descriptor, label=f"publication parent after tombstoning {label}")
        _assert_all(anchors, phase=f"after tombstoning {label}")
        stage.verify_payload(anchors=anchors)
        _assert_mutation_ready(anchors, phase=f"immediately before replacement publication for {label}", stage=stage)
        try:
            _renameat2(stage.descriptor, "payload", parent.descriptor, name, label=f"replacement {label}")
        except BaseException as exc:
            try:
                _restore_untrusted_prior(stage, parent, name, anchors=anchors)
            except BaseException as restore_exc:
                raise _fail(f"could not publish {label} and could not safely restore its tombstone", restore_exc) from restore_exc
            raise _fail(f"could not publish {label}", exc) from exc
        published = _trusted_leaf(parent.descriptor, name, label=label)
        if published is None or not _same_after_rename(published, staged):
            raise _fail(f"{label} changed during replacement publication")
        stage.close_payload()
        _fsync(stage.descriptor, label=f"private stage after publishing {label}")
        _fsync(parent.descriptor, label=f"publication parent after publishing {label}")
        _assert_all(anchors, phase=f"after publishing {label}")
        prior_now = _trusted_leaf(stage.descriptor, "prior", label=f"private tombstone for {label}")
        if prior_now is None or not _same_after_rename(prior_now, expected):
            raise _fail(f"private tombstone for {label} changed before disposal")
        _retain_trusted_prior(stage, expected, anchors=anchors)
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


def _require_managed_receipt_if_present(runtime: Anchor, *, anchors: tuple[Anchor, ...]) -> None:
    """Reject a regular foreign receipt before the transaction can tombstone it."""
    label = "build-receipt.json"
    expected = _trusted_leaf(runtime.descriptor, label, label=label)
    if expected is None:
        return
    raw = _read_regular(runtime, label, label=label, anchors=anchors)
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("build-receipt.json is not a managed TermDeck runtime receipt", exc) from exc
    required_files = {
        "bin/termdeck-web-server",
        "nginx/nginx.conf",
        "tls/ca.crt",
        "tls/server-chain.crt",
        "tls/server.crt",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"files", "https_port", "lan_ip", "ok", "schema"}
        or receipt.get("schema") != "termdeck.lan-runtime-build.v1"
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
    _assert_all(anchors, phase="after validating existing build receipt")
    observed = _trusted_leaf(runtime.descriptor, label, label=label)
    if observed != expected:
        raise _fail("build-receipt.json changed while validating its managed identity")


def _main() -> None:
    if len(sys.argv) != 4:
        raise _fail("invalid runtime publication invocation")
    runtime_path = sys.argv[1]
    lan_ip_object = ipaddress.ip_address(sys.argv[2])
    preflight_guards = sys.argv[3]
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
    anchors = (runtime, nginx, tls, binaries)
    try:
        _bind_preflight_guards(preflight_guards, runtime_path, anchors)
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
        files = {
            relative: hashlib.sha256(_read_regular(anchor, name, label=relative, anchors=anchors)).hexdigest()
            for relative, anchor, name in sources
        }
        receipt = {
            "files": files,
            "https_port": https_port,
            "lan_ip": lan_ip,
            "ok": True,
            "schema": "termdeck.lan-runtime-build.v1",
        }
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
        _require_managed_receipt_if_present(runtime, anchors=anchors)
        previous_receipt = _take_trusted_prior(
            runtime,
            "build-receipt.json",
            label="build-receipt.json",
            anchors=anchors,
        )
        try:
            _publish(nginx, "allowed-hosts.map", allowed_hosts, 0o600, label="nginx/allowed-hosts.map", anchors=anchors)
            _publish(runtime, "current-lan-ip", (lan_ip + "\n").encode("ascii"), 0o600, label="current-lan-ip", anchors=anchors)
            _publish(
                runtime,
                "build-receipt.json",
                (json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
                0o644,
                label="build-receipt.json",
                anchors=anchors,
            )
            if previous_receipt is not None:
                _retain_trusted_prior(previous_receipt[0], previous_receipt[1], anchors=anchors)
        finally:
            if previous_receipt is not None:
                previous_receipt[0].close()
    finally:
        for anchor in reversed(anchors):
            anchor.close()


try:
    _main()
except PublicationError as exc:
    sys.stderr.write(f"TermDeck runtime publication failed: {exc}\n")
    raise SystemExit(1)
PY
