"""Root-controlled attestations for a prepared TermDeck LAN runtime.

The LAN runtime is assembled by the dedicated ``termdeck-preparer`` account.
That account may ask this daemon to attest one already-published build receipt,
but it never reads the signing key.  The daemon only accepts a descriptor for
the canonical receipt path, proves that descriptor still names that path, and
records the request below a root-only control directory before replying.

The wire request deliberately reuses the build schema name.  It is a closed
envelope describing an already-created ``termdeck.lan-runtime-build.v2``
receipt, not a request to run arbitrary commands or sign arbitrary bytes.
The response is a detached Ed25519 signature which unprivileged activation
code can verify using the public key at ``/etc/termdeck/runtime-attestd.pub``.
"""

from __future__ import annotations

import argparse
import base64
import errno
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


REQUEST_SCHEMA: Final = "termdeck.lan-runtime-build.v2"
RESPONSE_SCHEMA: Final = "termdeck.runtime-attestation.v1"
RECORD_SCHEMA: Final = "termdeck.runtime-attestation-record.v1"
ERROR_SCHEMA: Final = "termdeck.runtime-attestation-error.v1"
OPERATION_SIGN: Final = "sign"
SIGNATURE_ALGORITHM: Final = "ed25519-openssl-pkeyutl.v1"

DEFAULT_CONTROL_ROOT: Final = Path("/etc/termdeck/control")
DEFAULT_SOCKET_PATH: Final = DEFAULT_CONTROL_ROOT / "runtime-attestd.sock"
DEFAULT_RECORDS_PATH: Final = DEFAULT_CONTROL_ROOT / "records"
DEFAULT_PRIVATE_KEY_PATH: Final = DEFAULT_CONTROL_ROOT / "runtime-attestd.key"
DEFAULT_PUBLIC_KEY_PATH: Final = Path("/etc/termdeck/runtime-attestd.pub")
DEFAULT_RUNTIME_RECEIPT_PATH: Final = Path(
    "/warp/runtime/termdeck/termdeck-lan/build-receipt.json"
)
DEFAULT_OPENSSL_PATH: Final = Path("/usr/bin/openssl")

CONTROL_DIRECTORY_MODE: Final = 0o710
RECORDS_DIRECTORY_MODE: Final = 0o700
SOCKET_MODE: Final = 0o660
PRIVATE_KEY_MODE: Final = 0o600
PUBLIC_KEY_MODE: Final = 0o644
RECORD_MODE: Final = 0o600
RECEIPT_MODE: Final = 0o644

MAX_FRAME_BYTES: Final = 128 * 1024
MAX_RECEIPT_BYTES: Final = 96 * 1024
MAX_RECORDS: Final = 1024
DEFAULT_CONNECTION_TIMEOUT: Final = 8.0
DEFAULT_MAX_CONNECTIONS: Final = 1024
_FRAME_HEADER_BYTES: Final = 4
_PEER_CREDENTIAL_BYTES: Final = struct.calcsize("3i")
_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}\Z")
_LOWER_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_NAME = re.compile(r"([0-9a-f]{32})\.json\Z")
_EXPECTED_RUNTIME_FILES: Final = frozenset(
    {
        "bin/termdeck-web-server",
        "nginx/nginx.conf",
        "tls/ca.crt",
        "tls/server-chain.crt",
        "tls/server.crt",
    }
)
_EXPECTED_RUNTIME_OUTPUTS: Final = frozenset(
    {"current-lan-ip", "nginx/allowed-hosts.map"}
)
_RUNTIME_CORE_FIELDS: Final = frozenset(
    {
        "files",
        "https_port",
        "lan_ip",
        "ok",
        "runtime_publication",
        "schema",
        "tls_publication",
    }
)
_RUNTIME_FINAL_FIELDS: Final = _RUNTIME_CORE_FIELDS | {"producer_attestation"}
_TLS_REQUIRED_FILES: Final = frozenset(
    {"ca.crt", "ca.key", "server.key", "server.crt", "server-chain.crt", "server.ext"}
)
_TLS_ALLOWED_FILES: Final = _TLS_REQUIRED_FILES | {"ca.srl"}
_TLS_TARGET_MODES: Final = {
    "server.key": 0o600,
    "server.crt": 0o644,
    "server-chain.crt": 0o644,
    "server.ext": 0o600,
}
_ED25519_PUBLIC_DER_PREFIX: Final = bytes.fromhex("302a300506032b6570032100")


class AttestationError(RuntimeError):
    """A request, control path, signature, or daemon invariant was unsafe."""


@dataclass(frozen=True)
class Principal:
    """The one unprivileged principal allowed to request attestations."""

    name: str
    uid: int
    gid: int


@dataclass(frozen=True)
class PeerCredentials:
    """Credentials obtained from Linux ``SO_PEERCRED`` rather than the wire."""

    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class DaemonConfig:
    """All filesystem and principal bindings for one attestor instance.

    Tests intentionally use disposable paths and a fixture principal.  The
    production factory below resolves the system accounts and uses root UID 0.
    """

    control_root: Path
    socket_path: Path
    records_path: Path
    private_key_path: Path
    public_key_path: Path
    runtime_receipt_path: Path
    preparer: Principal
    root_uid: int = 0
    root_gid: int = 0
    max_uid: int | None = None
    openssl_path: Path = DEFAULT_OPENSSL_PATH
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_receipt_bytes: int = MAX_RECEIPT_BYTES
    max_records: int = MAX_RECORDS
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT
    max_connections: int = DEFAULT_MAX_CONNECTIONS

    def __post_init__(self) -> None:
        if self.max_frame_bytes < 1024 or self.max_frame_bytes > 1024 * 1024:
            raise AttestationError("attestation frame limit is unsafe")
        if self.max_receipt_bytes < 1024 or self.max_receipt_bytes > self.max_frame_bytes:
            raise AttestationError("attestation receipt limit is unsafe")
        if not 1 <= self.max_records <= 16384:
            raise AttestationError("attestation record limit is unsafe")
        if not 1 <= self.max_connections <= 65536:
            raise AttestationError("attestation connection limit is unsafe")
        if self.connection_timeout <= 0 or self.connection_timeout > 60:
            raise AttestationError("attestation connection timeout is unsafe")
        if self.preparer.uid < 0 or self.preparer.gid < 0 or not self.preparer.name:
            raise AttestationError("attestation preparer identity is unsafe")
        if self.max_uid is not None and self.max_uid == self.preparer.uid:
            raise AttestationError("max cannot be the TermDeck preparer")
        _absolute_canonical(self.control_root, label="control root")
        _absolute_canonical(self.socket_path, label="attestation socket")
        _absolute_canonical(self.records_path, label="attestation records")
        _absolute_canonical(self.private_key_path, label="attestation private key")
        _absolute_canonical(self.public_key_path, label="attestation public key")
        _absolute_canonical(self.runtime_receipt_path, label="runtime receipt")
        if self.socket_path.parent != self.control_root:
            raise AttestationError("attestation socket must be directly below the control root")
        if self.records_path.parent != self.control_root:
            raise AttestationError("attestation records must be directly below the control root")
        if self.private_key_path.parent != self.control_root:
            raise AttestationError("attestation private key must be directly below the control root")
        if self.socket_path.name != "runtime-attestd.sock":
            raise AttestationError("attestation socket name is unsafe")
        if self.records_path.name != "records" or self.private_key_path.name != "runtime-attestd.key":
            raise AttestationError("attestation control leaf is unsafe")


def production_config() -> DaemonConfig:
    """Resolve the production root/preparer boundary from NSS.

    The explicit group-membership check prevents the ordinary ``max`` account
    from gaining traversal or socket-write access through a supplementary
    group.  Failure to resolve either account is a startup failure.
    """

    try:
        preparer_entry = pwd.getpwnam("termdeck-preparer")
        preparer_group = grp.getgrnam("termdeck-preparer")
        max_entry = pwd.getpwnam("max")
    except KeyError as exc:
        raise AttestationError("required TermDeck principal is unavailable") from exc
    if preparer_entry.pw_gid != preparer_group.gr_gid:
        raise AttestationError("termdeck-preparer must use its dedicated primary group")
    if preparer_entry.pw_uid == 0 or preparer_group.gr_gid == 0:
        raise AttestationError("termdeck-preparer must be separate from root")
    if max_entry.pw_uid == preparer_entry.pw_uid:
        raise AttestationError("max cannot be the TermDeck preparer")
    try:
        max_groups = set(os.getgrouplist(max_entry.pw_name, max_entry.pw_gid))
    except (AttributeError, OSError):
        max_groups = {max_entry.pw_gid}
    max_groups.update(
        group.gr_gid for group in grp.getgrall() if max_entry.pw_name in group.gr_mem
    )
    if preparer_group.gr_gid in max_groups:
        raise AttestationError("max must not be in the termdeck-preparer group")
    return DaemonConfig(
        control_root=DEFAULT_CONTROL_ROOT,
        socket_path=DEFAULT_SOCKET_PATH,
        records_path=DEFAULT_RECORDS_PATH,
        private_key_path=DEFAULT_PRIVATE_KEY_PATH,
        public_key_path=DEFAULT_PUBLIC_KEY_PATH,
        runtime_receipt_path=DEFAULT_RUNTIME_RECEIPT_PATH,
        preparer=Principal(
            name=preparer_entry.pw_name,
            uid=int(preparer_entry.pw_uid),
            gid=int(preparer_entry.pw_gid),
        ),
        max_uid=int(max_entry.pw_uid),
    )


def _absolute_canonical(value: str | os.PathLike[str], *, label: str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if not raw or not os.path.isabs(raw) or raw != os.path.normpath(raw):
        raise AttestationError(f"{label} must be an absolute canonical path")
    path = Path(raw)
    if path == Path("/") or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise AttestationError(f"{label} is unsafe")
    return path


def _component(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise AttestationError(f"{label} component is unsafe")
    return value


def _directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow:
        raise AttestationError("attestation requires O_DIRECTORY and O_NOFOLLOW")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _path_directory_flags() -> int:
    """Open a directory for traversal/stat only with execute permission alone."""

    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    path = getattr(os, "O_PATH", 0)
    if not directory or not nofollow or not path:
        raise AttestationError("attestation client requires O_PATH, O_DIRECTORY, and O_NOFOLLOW")
    return path | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _file_read_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise AttestationError("attestation requires O_NOFOLLOW")
    return os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)


def _file_write_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise AttestationError("attestation requires O_NOFOLLOW")
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)


def _file_read_write_create_flags() -> int:
    """Create a no-follow private temporary file that must be read back."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise AttestationError("attestation requires O_NOFOLLOW")
    return os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise AttestationError("control path is not a real directory")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_uid),
        int(info.st_gid),
    )


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise AttestationError("attestation leaf is not a regular file")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _identity_payload(info: os.stat_result) -> list[int]:
    return list(_file_identity(info))


def _assert_exact_directory(
    info: os.stat_result,
    *,
    mode: int,
    uid: int,
    gid: int,
    label: str,
) -> tuple[int, int, int, int, int]:
    identity = _directory_identity(info)
    if identity[2:] != (mode, uid, gid):
        raise AttestationError(f"{label} ownership or mode is unsafe")
    return identity


def _open_absolute_directory(path: Path, *, label: str, flags: int | None = None) -> int:
    """Open every directory component without trusting a pathname lookup."""

    checked = _absolute_canonical(path, label=label)
    directory_flags = _directory_flags() if flags is None else flags
    descriptor = os.open("/", directory_flags)
    try:
        _directory_identity(os.fstat(descriptor))
        for component in checked.parts[1:]:
            child = os.open(_component(component, label=label), directory_flags, dir_fd=descriptor)
            try:
                opened = _directory_identity(os.fstat(child))
                named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if _directory_identity(named)[:2] != opened[:2]:
                    raise AttestationError(f"{label} changed while opening")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent_and_leaf(path: Path, *, label: str, flags: int) -> tuple[int, str, int]:
    checked = _absolute_canonical(path, label=label)
    parent = _open_absolute_directory(checked.parent, label=f"{label} parent")
    name = _component(checked.name, label=label)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except BaseException:
        os.close(parent)
        raise
    return parent, name, descriptor


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise AttestationError("could not write durable attestation record")
        view = view[written:]


def _read_bounded(descriptor: int, *, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        try:
            chunk = os.read(descriptor, min(65536, remaining))
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) > limit:
        raise AttestationError(f"{label} exceeds its bounded size")
    return result


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AttestationError("attestation JSON is not canonicalizable") from exc


def _strict_json_object(content: bytes, *, label: str) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AttestationError(f"{label} has a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise AttestationError(f"{label} contains a non-finite JSON value")

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AttestationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AttestationError(f"{label} must be a JSON object")
    return value


def _int(value: object, *, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AttestationError(f"{label} is invalid")
    if maximum is not None and value > maximum:
        raise AttestationError(f"{label} is invalid")
    return value


def _lower_hex(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX.fullmatch(value) is None:
        raise AttestationError(f"{label} is invalid")
    return value


def _transaction_id(value: object) -> str:
    if not isinstance(value, str) or _TRANSACTION_ID.fullmatch(value) is None:
        raise AttestationError("attestation transaction id is invalid")
    return value


def _identity_from_payload(value: object, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if not isinstance(value, list) or len(value) != 7:
        raise AttestationError(f"{label} is invalid")
    parsed = tuple(_int(item, label=label) for item in value)
    if parsed[1] <= 0 or parsed[3] != 1 or parsed[2] != RECEIPT_MODE:
        raise AttestationError(f"{label} is invalid")
    return parsed  # type: ignore[return-value]


def _publication_identity(value: object, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    if not isinstance(value, list) or len(value) != 7:
        raise AttestationError(f"{label} is invalid")
    parsed = tuple(_int(item, label=label) for item in value)
    if parsed[1] <= 0 or parsed[3] != 1:
        raise AttestationError(f"{label} is invalid")
    return parsed  # type: ignore[return-value]


def _tls_directory_identity(value: object, *, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise AttestationError(f"{label} is invalid")
    parsed = tuple(_int(item, label=label) for item in value)
    if parsed[1] <= 0 or parsed[2] != 0o700:
        raise AttestationError(f"{label} is invalid")
    return parsed  # type: ignore[return-value]


def _validate_tls_manifest(value: object, *, label: str) -> None:
    if not isinstance(value, dict) or not _TLS_REQUIRED_FILES.issubset(value) or set(value) - _TLS_ALLOWED_FILES:
        raise AttestationError(f"{label} is invalid")
    for name in sorted(value):
        entry = value[name]
        if not isinstance(entry, dict) or set(entry) != {"identity", "mode", "sha256"}:
            raise AttestationError(f"{label} is invalid")
        identity = _publication_identity(entry["identity"], label=label)
        mode = _int(entry["mode"], label=label, maximum=0o777)
        if mode != identity[2] or _lower_hex(entry["sha256"], label=label) is None:
            raise AttestationError(f"{label} is invalid")
        expected_mode = _TLS_TARGET_MODES.get(name)
        if expected_mode is not None and mode != expected_mode:
            raise AttestationError(f"{label} is invalid")


def _validate_tls_publication(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"generation", "current", "prior"}:
        raise AttestationError("runtime receipt TLS publication is invalid")
    generation = value["generation"]
    if not isinstance(generation, str) or _TRANSACTION_ID.fullmatch(generation) is None:
        raise AttestationError("runtime receipt TLS publication is invalid")
    current = value["current"]
    if not isinstance(current, dict) or set(current) != {"path", "identity", "manifest"}:
        raise AttestationError("runtime receipt TLS publication is invalid")
    if current.get("path") != "tls":
        raise AttestationError("runtime receipt TLS publication is invalid")
    _tls_directory_identity(current.get("identity"), label="TLS current identity")
    _validate_tls_manifest(current.get("manifest"), label="TLS current manifest")
    prior = value["prior"]
    if prior is None:
        return
    if not isinstance(prior, dict) or set(prior) != {
        "journal", "journal_identity", "path", "identity", "manifest"
    }:
        raise AttestationError("runtime receipt TLS publication is invalid")
    if prior.get("journal") != ".termdeck-tls-transaction-" + generation or prior.get("path") != "prior-tls":
        raise AttestationError("runtime receipt TLS publication is invalid")
    _tls_directory_identity(prior.get("journal_identity"), label="TLS journal identity")
    _tls_directory_identity(prior.get("identity"), label="TLS prior identity")
    _validate_tls_manifest(prior.get("manifest"), label="TLS prior manifest")


def _runtime_generation(receipt: Mapping[str, object]) -> str:
    """Return the one generation identifier shared by runtime and TLS state."""

    publication = receipt.get("runtime_publication")
    tls_publication = receipt.get("tls_publication")
    if not isinstance(publication, Mapping) or not isinstance(tls_publication, Mapping):
        raise AttestationError("runtime receipt publication is invalid")
    runtime_generation = publication.get("generation")
    tls_generation = tls_publication.get("generation")
    if (
        not isinstance(runtime_generation, str)
        or _TRANSACTION_ID.fullmatch(runtime_generation) is None
        or runtime_generation != tls_generation
    ):
        raise AttestationError("runtime and TLS publication generations do not match")
    return runtime_generation


def validate_runtime_build_core(receipt: object) -> dict[str, object]:
    """Validate the closed unsigned projection of a v2 runtime receipt.

    The producer asks the daemon to sign this projection while it is staged at
    the canonical receipt path.  The final published receipt adds exactly one
    field, ``producer_attestation``.  Keeping that field out of the signed
    projection avoids a self-referential receipt hash.
    """

    if not isinstance(receipt, dict):
        raise AttestationError("runtime receipt must be an object")
    if set(receipt) != _RUNTIME_CORE_FIELDS or receipt.get("schema") != REQUEST_SCHEMA or receipt.get("ok") is not True:
        raise AttestationError("runtime receipt schema is invalid")
    port = _int(receipt.get("https_port"), label="runtime receipt HTTPS port", minimum=1, maximum=65535)
    del port
    lan_ip = receipt.get("lan_ip")
    if not isinstance(lan_ip, str):
        raise AttestationError("runtime receipt LAN address is invalid")
    try:
        address = ipaddress.ip_address(lan_ip)
    except ValueError as exc:
        raise AttestationError("runtime receipt LAN address is invalid") from exc
    private = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"))
    if address.version != 4 or not any(address in network for network in private):
        raise AttestationError("runtime receipt LAN address is invalid")
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != _EXPECTED_RUNTIME_FILES:
        raise AttestationError("runtime receipt files are invalid")
    for name in sorted(files):
        _lower_hex(files[name], label=f"runtime receipt file {name}")
    publication = receipt.get("runtime_publication")
    if not isinstance(publication, dict) or set(publication) != {"generation", "outputs"}:
        raise AttestationError("runtime receipt publication is invalid")
    generation = publication.get("generation")
    if not isinstance(generation, str) or _TRANSACTION_ID.fullmatch(generation) is None:
        raise AttestationError("runtime receipt publication is invalid")
    outputs = publication.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != _EXPECTED_RUNTIME_OUTPUTS:
        raise AttestationError("runtime receipt publication is invalid")
    for name in sorted(outputs):
        entry = outputs[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "mode", "identity"}:
            raise AttestationError("runtime receipt publication is invalid")
        if entry.get("path") != name or _lower_hex(entry.get("sha256"), label="runtime output digest") is None:
            raise AttestationError("runtime receipt publication is invalid")
        mode = _int(entry.get("mode"), label="runtime output mode", maximum=0o777)
        identity = _publication_identity(entry.get("identity"), label="runtime output identity")
        if mode != 0o600 or identity[2] != mode:
            raise AttestationError("runtime receipt publication is invalid")
    _validate_tls_publication(receipt.get("tls_publication"))
    _runtime_generation(receipt)
    return receipt


def validate_runtime_build_receipt(receipt: object) -> dict[str, object]:
    """Backward-compatible name for validation of an unsigned v2 core."""

    return validate_runtime_build_core(receipt)


def validate_request(value: object, *, config: DaemonConfig) -> dict[str, object]:
    """Validate the closed on-wire request before touching a receipt FD."""

    if not isinstance(value, dict) or set(value) != {"schema", "operation", "transaction"}:
        raise AttestationError("attestation request schema is invalid")
    if value.get("schema") != REQUEST_SCHEMA or value.get("operation") != OPERATION_SIGN:
        raise AttestationError("attestation request schema is invalid")
    transaction = value.get("transaction")
    expected = {"id", "receipt_path", "receipt_core_sha256", "receipt_core_identity"}
    if not isinstance(transaction, dict) or set(transaction) != expected:
        raise AttestationError("attestation transaction is invalid")
    _transaction_id(transaction.get("id"))
    path = transaction.get("receipt_path")
    if not isinstance(path, str) or path != str(config.runtime_receipt_path):
        raise AttestationError("attestation transaction receipt path is invalid")
    _lower_hex(transaction.get("receipt_core_sha256"), label="attestation receipt core digest")
    _identity_from_payload(transaction.get("receipt_core_identity"), label="attestation receipt core identity")
    return value


def _request_fields(request: Mapping[str, object]) -> tuple[str, str, str, tuple[int, int, int, int, int, int, int]]:
    transaction = request["transaction"]
    assert isinstance(transaction, Mapping)
    transaction_id = _transaction_id(transaction["id"])
    digest = _lower_hex(transaction["receipt_core_sha256"], label="attestation receipt core digest")
    path = transaction["receipt_path"]
    assert isinstance(path, str)
    identity = _identity_from_payload(transaction["receipt_core_identity"], label="attestation receipt core identity")
    return transaction_id, path, digest, identity


def _read_runtime_receipt(
    config: DaemonConfig,
    request: Mapping[str, object],
    received_descriptor: int,
) -> tuple[bytes, tuple[int, int, int, int, int, int, int]]:
    """Bind an SCM_RIGHTS descriptor to the daemon's exact receipt pathname."""

    transaction_id, receipt_path, expected_digest, expected_identity = _request_fields(request)
    if receipt_path != str(config.runtime_receipt_path):
        raise AttestationError("attestation receipt path changed")
    try:
        supplied = _file_identity(os.fstat(received_descriptor))
    except OSError as exc:
        raise AttestationError("attestation receipt descriptor is unavailable") from exc
    if supplied != expected_identity:
        raise AttestationError("attestation receipt descriptor does not match transaction")
    if supplied[4] > config.max_receipt_bytes:
        raise AttestationError("attestation receipt exceeds its bounded size")
    parent, name, descriptor = _open_parent_and_leaf(
        config.runtime_receipt_path,
        label="attested runtime receipt",
        flags=_file_read_flags(),
    )
    try:
        parent_info = os.fstat(parent)
        if (
            stat.S_IMODE(parent_info.st_mode) != 0o700
            or int(parent_info.st_uid) != config.preparer.uid
            or int(parent_info.st_gid) != config.preparer.gid
        ):
            raise AttestationError("attested runtime receipt parent ownership or mode is unsafe")
        named_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        opened_before = _file_identity(os.fstat(descriptor))
        if opened_before != supplied or _file_identity(named_before) != supplied:
            raise AttestationError("attested runtime receipt is no longer the supplied descriptor")
        if opened_before[2] != RECEIPT_MODE or opened_before[3] != 1 or opened_before[4] > config.max_receipt_bytes:
            raise AttestationError("attested runtime receipt is unsafe")
        if int(os.fstat(descriptor).st_uid) != config.preparer.uid:
            raise AttestationError("attested runtime receipt is not owned by the preparer")
        content = _read_bounded(descriptor, limit=config.max_receipt_bytes, label="attested runtime receipt")
        opened_after = _file_identity(os.fstat(descriptor))
        named_after = _file_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        if opened_after != opened_before or named_after != opened_before:
            raise AttestationError("attested runtime receipt changed while reading")
    finally:
        os.close(descriptor)
        os.close(parent)
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise AttestationError("attested runtime receipt digest does not match transaction")
    receipt = _strict_json_object(content, label="attested runtime receipt")
    if content != _canonical_json(receipt):
        raise AttestationError("attested runtime receipt is not canonical")
    validate_runtime_build_core(receipt)
    if transaction_id != _runtime_generation(receipt):
        raise AttestationError("attestation transaction is not bound to the runtime generation")
    return content, supplied


def build_request(
    *,
    transaction_id: str,
    receipt_path: str | Path,
    receipt_descriptor: int,
    config: DaemonConfig,
) -> dict[str, object]:
    """Create a canonical request tied to one opened receipt descriptor."""

    transaction_id = _transaction_id(transaction_id)
    path = _absolute_canonical(receipt_path, label="receipt path")
    if path != config.runtime_receipt_path:
        raise AttestationError("receipt path is not the configured runtime receipt")
    try:
        before = _file_identity(os.fstat(receipt_descriptor))
    except OSError as exc:
        raise AttestationError("receipt descriptor is unavailable") from exc
    if before[2] != RECEIPT_MODE or before[3] != 1 or before[4] > config.max_receipt_bytes:
        raise AttestationError("receipt descriptor is unsafe")
    try:
        original_offset = os.lseek(receipt_descriptor, 0, os.SEEK_CUR)
        os.lseek(receipt_descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise AttestationError("receipt descriptor is not seekable") from exc
    try:
        content = _read_bounded(receipt_descriptor, limit=config.max_receipt_bytes, label="runtime receipt")
    finally:
        try:
            os.lseek(receipt_descriptor, original_offset, os.SEEK_SET)
        except OSError:
            pass
    try:
        after = _file_identity(os.fstat(receipt_descriptor))
    except OSError as exc:
        raise AttestationError("receipt descriptor disappeared") from exc
    if before != after:
        raise AttestationError("receipt descriptor changed while reading")
    receipt = _strict_json_object(content, label="runtime receipt")
    if content != _canonical_json(receipt):
        raise AttestationError("runtime receipt is not canonical")
    validate_runtime_build_core(receipt)
    if transaction_id != _runtime_generation(receipt):
        raise AttestationError("attestation transaction is not bound to the runtime generation")
    return {
        "schema": REQUEST_SCHEMA,
        "operation": OPERATION_SIGN,
        "transaction": {
            "id": transaction_id,
            "receipt_path": str(path),
            "receipt_core_sha256": hashlib.sha256(content).hexdigest(),
            "receipt_core_identity": list(before),
        },
    }


def _peer_credentials(connection: socket.socket) -> PeerCredentials:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_CREDENTIAL_BYTES)
    except (AttributeError, OSError) as exc:
        raise AttestationError("SO_PEERCRED is unavailable") from exc
    if len(raw) != _PEER_CREDENTIAL_BYTES:
        raise AttestationError("SO_PEERCRED is malformed")
    pid, uid, gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0 or gid < 0:
        raise AttestationError("SO_PEERCRED is invalid")
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _assert_peer(peer: PeerCredentials, *, config: DaemonConfig) -> None:
    if peer.uid != config.preparer.uid or peer.gid != config.preparer.gid:
        raise AttestationError("attestation peer is not termdeck-preparer")
    if config.max_uid is not None and peer.uid == config.max_uid:
        raise AttestationError("max is not allowed to request a runtime attestation")


def _assert_client_principal(config: DaemonConfig) -> None:
    uid = os.geteuid()
    gid = os.getegid()
    if config.max_uid is not None and uid == config.max_uid:
        raise AttestationError("max must not connect to the attestation socket")
    if uid != config.preparer.uid or gid != config.preparer.gid:
        raise AttestationError("attestation client must run as termdeck-preparer")


def _control_root_descriptor(config: DaemonConfig, *, traversal_only: bool = False) -> int:
    """Open the control root safely.

    The preparer has group execute-only access to the 0710 root.  It uses an
    ``O_PATH`` descriptor solely to inspect/connect the known socket name;
    the root daemon uses a readable descriptor for records and ``fsync``.
    """

    descriptor = _open_absolute_directory(
        config.control_root,
        label="attestation control root",
        flags=_path_directory_flags() if traversal_only else None,
    )
    try:
        _assert_exact_directory(
            os.fstat(descriptor),
            mode=CONTROL_DIRECTORY_MODE,
            uid=config.root_uid,
            gid=config.preparer.gid,
            label="attestation control root",
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _records_descriptor(config: DaemonConfig, control_descriptor: int) -> int:
    descriptor = os.open("records", _directory_flags(), dir_fd=control_descriptor)
    try:
        _assert_exact_directory(
            os.fstat(descriptor),
            mode=RECORDS_DIRECTORY_MODE,
            uid=config.root_uid,
            gid=config.root_gid,
            label="attestation records directory",
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_key(config: DaemonConfig, control_descriptor: int) -> int:
    descriptor = os.open("runtime-attestd.key", _file_read_flags(), dir_fd=control_descriptor)
    try:
        info = os.fstat(descriptor)
        identity = _file_identity(info)
        if (
            identity[2] != PRIVATE_KEY_MODE
            or identity[3] != 1
            or int(info.st_uid) != config.root_uid
            or int(info.st_gid) != config.root_gid
            or identity[4] <= 0
            or identity[4] > 16 * 1024
        ):
            raise AttestationError("attestation private key is unsafe")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_public_key(config: DaemonConfig) -> int:
    parent, name, descriptor = _open_parent_and_leaf(
        config.public_key_path,
        label="attestation public key",
        flags=_file_read_flags(),
    )
    try:
        parent_info = os.fstat(parent)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or int(parent_info.st_uid) != config.root_uid
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise AttestationError("attestation public key parent is unsafe")
        info = os.fstat(descriptor)
        identity = _file_identity(info)
        named = _file_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        if (
            named != identity
            or identity[2] not in {0o444, PUBLIC_KEY_MODE}
            or identity[3] != 1
            or int(info.st_uid) != config.root_uid
            or int(info.st_gid) != config.root_gid
            or identity[4] <= 0
            or identity[4] > 16 * 1024
        ):
            raise AttestationError("attestation public key is unsafe")
    except BaseException:
        os.close(descriptor)
        os.close(parent)
        raise
    os.close(parent)
    return descriptor


def _openssl(config: DaemonConfig, arguments: Sequence[str], *, input_data: bytes | None = None, pass_fds: Sequence[int] = ()) -> bytes:
    try:
        executable = os.fspath(config.openssl_path)
        result = subprocess.run(
            [executable, *arguments],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            pass_fds=tuple(pass_fds),
            timeout=8,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError("attestation cryptographic helper failed") from exc
    if result.returncode != 0:
        raise AttestationError("attestation cryptographic helper rejected its input")
    return bytes(result.stdout)


def _public_der_from_private(config: DaemonConfig, private_descriptor: int) -> bytes:
    try:
        os.lseek(private_descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise AttestationError("attestation private key is not seekable") from exc
    result = _openssl(
        config,
        [
            "pkey",
            "-in",
            f"/proc/self/fd/{private_descriptor}",
            "-pubout",
            "-outform",
            "DER",
        ],
        pass_fds=(private_descriptor,),
    )
    _validate_ed25519_public_der(result)
    return result


def _public_der_from_public(config: DaemonConfig, public_descriptor: int) -> bytes:
    try:
        os.lseek(public_descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise AttestationError("attestation public key is not seekable") from exc
    result = _openssl(
        config,
        [
            "pkey",
            "-pubin",
            "-in",
            f"/proc/self/fd/{public_descriptor}",
            "-pubout",
            "-outform",
            "DER",
        ],
        pass_fds=(public_descriptor,),
    )
    _validate_ed25519_public_der(result)
    return result


def _validate_ed25519_public_der(value: bytes) -> None:
    if len(value) != len(_ED25519_PUBLIC_DER_PREFIX) + 32 or not value.startswith(_ED25519_PUBLIC_DER_PREFIX):
        raise AttestationError("attestation key is not Ed25519")


def _signature_payload(response: Mapping[str, object]) -> bytes:
    attestation = response.get("attestation")
    if not isinstance(attestation, Mapping):
        raise AttestationError("attestation response is invalid")
    payload = {
        "schema": response.get("schema"),
        "transaction_id": response.get("transaction_id"),
        "receipt_path": response.get("receipt_path"),
        "receipt_core_sha256": response.get("receipt_core_sha256"),
        "receipt_core_identity": response.get("receipt_core_identity"),
        "request_sha256": response.get("request_sha256"),
        "issued_at_ns": response.get("issued_at_ns"),
        "attestation": {
            "algorithm": attestation.get("algorithm"),
            "key_id": attestation.get("key_id"),
        },
    }
    return _canonical_json(payload)


def _sign_payload(config: DaemonConfig, private_descriptor: int, payload: bytes) -> bytes:
    try:
        os.lseek(private_descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise AttestationError("attestation private key is not seekable") from exc
    # Ed25519 is a one-shot operation: OpenSSL must know the complete input
    # size before reading it.  A stdin pipe cannot provide that size.
    payload_descriptor = _memory_descriptor("termdeck-attestation-payload", payload)
    try:
        signature = _openssl(
            config,
            [
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                f"/proc/self/fd/{private_descriptor}",
                "-in",
                f"/proc/self/fd/{payload_descriptor}",
            ],
            pass_fds=(private_descriptor, payload_descriptor),
        )
    finally:
        os.close(payload_descriptor)
    if len(signature) != 64:
        raise AttestationError("attestation signer did not emit an Ed25519 signature")
    return signature


def _signature_from_text(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise AttestationError("attestation signature is invalid")
    try:
        signature = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise AttestationError("attestation signature is invalid") from exc
    if len(signature) != 64:
        raise AttestationError("attestation signature is invalid")
    return signature


def validate_response(
    value: object,
    *,
    request: Mapping[str, object] | None = None,
    config: DaemonConfig | None = None,
) -> dict[str, object]:
    """Validate fields and request binding before signature verification."""

    expected = {
        "schema", "transaction_id", "receipt_path", "receipt_core_sha256", "receipt_core_identity",
        "request_sha256", "issued_at_ns", "attestation",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != RESPONSE_SCHEMA:
        raise AttestationError("attestation response schema is invalid")
    transaction_id = _transaction_id(value.get("transaction_id"))
    path = value.get("receipt_path")
    if not isinstance(path, str):
        raise AttestationError("attestation response receipt path is invalid")
    _absolute_canonical(path, label="attestation response receipt path")
    digest = _lower_hex(value.get("receipt_core_sha256"), label="attestation response receipt core digest")
    identity = _identity_from_payload(value.get("receipt_core_identity"), label="attestation response receipt core identity")
    request_digest = _lower_hex(value.get("request_sha256"), label="attestation response request digest")
    issued = _int(value.get("issued_at_ns"), label="attestation response issue time", minimum=1)
    if config is not None:
        if path != str(config.runtime_receipt_path):
            raise AttestationError("attestation response receipt path is invalid")
    attestation = value.get("attestation")
    if not isinstance(attestation, dict) or set(attestation) != {"algorithm", "key_id", "signature"}:
        raise AttestationError("attestation response signature is invalid")
    if attestation.get("algorithm") != SIGNATURE_ALGORITHM:
        raise AttestationError("attestation response signature is invalid")
    _lower_hex(attestation.get("key_id"), label="attestation key id")
    _signature_from_text(attestation.get("signature"))
    if request is not None:
        normalized = validate_request(request, config=config if config is not None else _config_from_request_path(path))
        expected_transaction, expected_path, expected_digest, expected_identity = _request_fields(normalized)
        if (
            transaction_id != expected_transaction
            or path != expected_path
            or digest != expected_digest
            or identity != expected_identity
            or request_digest != hashlib.sha256(_canonical_json(normalized)).hexdigest()
        ):
            raise AttestationError("attestation response does not bind its request")
    return value


def _config_from_request_path(path: str) -> DaemonConfig:
    """Small validator-only config used when callers do not need a socket."""

    return DaemonConfig(
        control_root=Path("/tmp/termdeck-attestation-validator-control"),
        socket_path=Path("/tmp/termdeck-attestation-validator-control/runtime-attestd.sock"),
        records_path=Path("/tmp/termdeck-attestation-validator-control/records"),
        private_key_path=Path("/tmp/termdeck-attestation-validator-control/runtime-attestd.key"),
        public_key_path=Path("/tmp/termdeck-attestation-validator.pub"),
        runtime_receipt_path=Path(path),
        preparer=Principal("validator", 1, 1),
        root_uid=1,
        root_gid=1,
    )


def verify_response_signature(
    response: Mapping[str, object],
    *,
    request: Mapping[str, object],
    config: DaemonConfig,
    now_ns: int | None = None,
) -> None:
    """Verify an attestation without reading the root-only private key."""

    normalized = validate_response(response, request=request, config=config)
    del now_ns  # Durable receipts must remain verifiable across service restarts.
    public_descriptor = _open_public_key(config)
    signature_descriptor = -1
    payload_descriptor = -1
    try:
        public_der = _public_der_from_public(config, public_descriptor)
        attestation = normalized["attestation"]
        assert isinstance(attestation, Mapping)
        if hashlib.sha256(public_der).hexdigest() != attestation["key_id"]:
            raise AttestationError("attestation response uses an unexpected public key")
        signature = _signature_from_text(attestation["signature"])
        signature_descriptor = _memory_descriptor("termdeck-attestation-signature", signature)
        payload_descriptor = _memory_descriptor(
            "termdeck-attestation-payload", _signature_payload(normalized)
        )
        try:
            os.lseek(public_descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise AttestationError("attestation public key is not seekable") from exc
        result = _openssl(
            config,
            [
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{public_descriptor}",
                "-sigfile",
                f"/proc/self/fd/{signature_descriptor}",
                "-in",
                f"/proc/self/fd/{payload_descriptor}",
            ],
            pass_fds=(public_descriptor, signature_descriptor, payload_descriptor),
        )
        del result
    finally:
        if payload_descriptor >= 0:
            os.close(payload_descriptor)
        if signature_descriptor >= 0:
            os.close(signature_descriptor)
        os.close(public_descriptor)


def _runtime_relative_components(value: str) -> tuple[str, ...]:
    """Return a strict runtime-relative path suitable for ``dir_fd`` opens."""

    if (
        not value
        or value.startswith("/")
        or value != os.path.normpath(value)
        or "\x00" in value
    ):
        raise AttestationError("runtime-relative path is unsafe")
    components = tuple(value.split("/"))
    if any(
        not component
        or component in {".", ".."}
        or "\\" in component
        for component in components
    ):
        raise AttestationError("runtime-relative path is unsafe")
    return components


def _open_runtime_relative(
    runtime_descriptor: int,
    relative_path: str,
    *,
    directory: bool,
) -> int:
    """Open a runtime child without ever re-resolving the runtime pathname."""

    components = _runtime_relative_components(relative_path)
    try:
        runtime_info = os.fstat(runtime_descriptor)
        _directory_identity(runtime_info)
    except OSError as exc:
        raise AttestationError("runtime parent is unavailable") from exc
    if stat.S_IMODE(runtime_info.st_mode) != 0o700:
        raise AttestationError("runtime parent mode is unsafe")
    owner_uid = int(runtime_info.st_uid)
    owner_gid = int(runtime_info.st_gid)

    def assert_runtime_ancestor(info: os.stat_result, *, label: str) -> None:
        _directory_identity(info)
        if int(info.st_uid) != owner_uid or int(info.st_gid) != owner_gid:
            raise AttestationError(f"{label} is not owned by the runtime preparer")

    current = os.dup(runtime_descriptor)
    try:
        for component in components[:-1]:
            child = os.open(component, _directory_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                assert_runtime_ancestor(
                    opened,
                    label=f"runtime ancestor {component}",
                )
                named = os.stat(component, dir_fd=current, follow_symlinks=False)
                if _directory_identity(named)[:2] != _directory_identity(opened)[:2]:
                    raise AttestationError("runtime ancestor changed while opening")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        flags = _directory_flags() if directory else _file_read_flags()
        child = os.open(components[-1], flags, dir_fd=current)
        try:
            if directory:
                opened = os.fstat(child)
                assert_runtime_ancestor(
                    opened,
                    label=f"runtime directory {components[-1]}",
                )
                named = os.stat(components[-1], dir_fd=current, follow_symlinks=False)
                if _directory_identity(named)[:2] != _directory_identity(opened)[:2]:
                    raise AttestationError("runtime directory changed while opening")
            else:
                opened = os.fstat(child)
                _file_identity(opened)
                if int(opened.st_uid) != owner_uid or int(opened.st_gid) != owner_gid:
                    raise AttestationError(
                        f"runtime leaf {components[-1]} is not owned by the runtime preparer"
                    )
        except BaseException:
            os.close(child)
            raise
        return child
    finally:
        os.close(current)


@dataclass
class VerifiedRuntimeReceiptLease:
    """Verified receipt bytes plus retained descriptors for descriptor-safe use.

    The lease intentionally holds the directory that contained the final
    receipt while it was parsed and signature-checked.  Consumers open each
    output and TLS leaf through the lease, validate that leaf's manifest
    identity/hash from that same descriptor, and never go back to a pathname.
    ``revalidate_receipt`` detects replacement or ordinary mutation before a
    consumer crosses its own outer activation checkpoint.
    """

    receipt: dict[str, object]
    core: dict[str, object]
    producer_attestation: dict[str, object]
    _runtime_descriptor: int
    _receipt_descriptor: int
    _receipt_name: str
    _receipt_identity: tuple[int, int, int, int, int, int, int]
    _runtime_owner_uid: int
    _runtime_owner_gid: int
    _closed: bool = False

    def __enter__(self) -> "VerifiedRuntimeReceiptLease":
        self._assert_open()
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise AttestationError("verified runtime receipt lease is closed")

    @property
    def receipt_identity(self) -> tuple[int, int, int, int, int, int, int]:
        """Return the final receipt identity captured by the verified lease."""

        self._assert_open()
        return self._receipt_identity

    def revalidate_receipt(self) -> None:
        """Require the final receipt descriptor and its retained directory entry to match."""

        self._assert_open()
        try:
            runtime_info = os.fstat(self._runtime_descriptor)
            if (
                stat.S_IMODE(runtime_info.st_mode) != 0o700
                or int(runtime_info.st_uid) != self._runtime_owner_uid
                or int(runtime_info.st_gid) != self._runtime_owner_gid
            ):
                raise AttestationError("final runtime receipt parent ownership or mode changed")
            descriptor_identity = _file_identity(os.fstat(self._receipt_descriptor))
            named_identity = _file_identity(
                os.stat(self._receipt_name, dir_fd=self._runtime_descriptor, follow_symlinks=False)
            )
        except OSError as exc:
            raise AttestationError("verified runtime receipt disappeared") from exc
        if descriptor_identity != self._receipt_identity or named_identity != self._receipt_identity:
            raise AttestationError("verified runtime receipt changed after attestation")

    def open_runtime_leaf(self, relative_path: str) -> int:
        """Open a regular output leaf below the retained runtime directory."""

        self._assert_open()
        self.revalidate_receipt()
        try:
            return _open_runtime_relative(self._runtime_descriptor, relative_path, directory=False)
        except OSError as exc:
            raise AttestationError("runtime output leaf is unavailable") from exc

    def open_runtime_directory(self, relative_path: str) -> int:
        """Open a runtime directory below the retained runtime directory."""

        self._assert_open()
        self.revalidate_receipt()
        try:
            return _open_runtime_relative(self._runtime_descriptor, relative_path, directory=True)
        except OSError as exc:
            raise AttestationError("runtime output directory is unavailable") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (self._receipt_descriptor, self._runtime_descriptor):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _core_request_from_response(
    core: Mapping[str, object], response: Mapping[str, object]
) -> dict[str, object]:
    """Reconstruct the one closed request which the response must bind."""

    transaction_id = _transaction_id(response.get("transaction_id"))
    if transaction_id != _runtime_generation(core):
        raise AttestationError("attestation response is not bound to the runtime generation")
    receipt_path = response.get("receipt_path")
    if not isinstance(receipt_path, str):
        raise AttestationError("attestation response receipt path is invalid")
    digest = hashlib.sha256(_canonical_json(core)).hexdigest()
    if digest != _lower_hex(response.get("receipt_core_sha256"), label="attestation receipt core digest"):
        raise AttestationError("attestation response does not bind the final receipt core")
    identity = response.get("receipt_core_identity")
    _identity_from_payload(identity, label="attestation response receipt core identity")
    return {
        "schema": REQUEST_SCHEMA,
        "operation": OPERATION_SIGN,
        "transaction": {
            "id": transaction_id,
            "receipt_path": receipt_path,
            "receipt_core_sha256": digest,
            "receipt_core_identity": identity,
        },
    }


def verify_runtime_build_attestation(
    receipt: object,
    *,
    config: DaemonConfig,
) -> dict[str, object]:
    """Verify the final core-plus-attestation receipt with the public key.

    ``receipt`` must be parsed with duplicate-key rejection when it originates
    from bytes.  :func:`open_verified_runtime_receipt` provides that parsing
    and a descriptor-held lease for activation consumers.
    """

    if not isinstance(receipt, dict) or set(receipt) != _RUNTIME_FINAL_FIELDS:
        raise AttestationError("final runtime receipt schema is invalid")
    core = {name: receipt[name] for name in _RUNTIME_CORE_FIELDS}
    validate_runtime_build_core(core)
    response = receipt.get("producer_attestation")
    if not isinstance(response, dict):
        raise AttestationError("final runtime receipt attestation is invalid")
    request = _core_request_from_response(core, response)
    verify_response_signature(response, request=request, config=config)
    return receipt


def open_verified_runtime_receipt(*, config: DaemonConfig) -> VerifiedRuntimeReceiptLease:
    """Open, parse, and verify the final receipt without a pathname TOCTOU.

    The caller owns the returned lease and must close it.  It should perform
    all manifest-bound leaf consumption through the lease before closing it.
    """

    runtime_descriptor, receipt_name, receipt_descriptor = _open_parent_and_leaf(
        config.runtime_receipt_path,
        label="final runtime receipt",
        flags=_file_read_flags(),
    )
    try:
        runtime_info = os.fstat(runtime_descriptor)
        if (
            stat.S_IMODE(runtime_info.st_mode) != 0o700
            or int(runtime_info.st_uid) != config.preparer.uid
            or int(runtime_info.st_gid) != config.preparer.gid
        ):
            raise AttestationError("final runtime receipt parent ownership or mode is unsafe")
        before = _file_identity(os.fstat(receipt_descriptor))
        named_before = _file_identity(
            os.stat(receipt_name, dir_fd=runtime_descriptor, follow_symlinks=False)
        )
        if (
            before != named_before
            or before[2] != RECEIPT_MODE
            or before[3] != 1
            or before[4] > config.max_receipt_bytes
            or int(os.fstat(receipt_descriptor).st_uid) != config.preparer.uid
        ):
            raise AttestationError("final runtime receipt is unsafe")
        content = _read_bounded(
            receipt_descriptor,
            limit=config.max_receipt_bytes,
            label="final runtime receipt",
        )
        after = _file_identity(os.fstat(receipt_descriptor))
        named_after = _file_identity(
            os.stat(receipt_name, dir_fd=runtime_descriptor, follow_symlinks=False)
        )
        if before != after or before != named_after:
            raise AttestationError("final runtime receipt changed while reading")
        receipt = _strict_json_object(content, label="final runtime receipt")
        if content != _canonical_json(receipt):
            raise AttestationError("final runtime receipt is not canonical")
        verified = verify_runtime_build_attestation(receipt, config=config)
        core = {name: verified[name] for name in _RUNTIME_CORE_FIELDS}
        attestation = verified["producer_attestation"]
        assert isinstance(attestation, dict)
        signed_identity = _identity_from_payload(
            attestation.get("receipt_core_identity"),
            label="attestation response receipt core identity",
        )
        # Appending the detached response changes size and timestamps.  The
        # final receipt must still be the same single-linked regular inode
        # which the broker observed at the canonical path before signing.
        if before[:4] != signed_identity[:4]:
            raise AttestationError("final runtime receipt identity does not match its signed core")
        lease = VerifiedRuntimeReceiptLease(
            receipt=verified,
            core=core,
            producer_attestation=attestation,
            _runtime_descriptor=runtime_descriptor,
            _receipt_descriptor=receipt_descriptor,
            _receipt_name=receipt_name,
            _receipt_identity=before,
            _runtime_owner_uid=int(runtime_info.st_uid),
            _runtime_owner_gid=int(runtime_info.st_gid),
        )
        lease.revalidate_receipt()
        return lease
    except BaseException:
        os.close(receipt_descriptor)
        os.close(runtime_descriptor)
        raise


def _memory_descriptor(name: str, content: bytes) -> int:
    try:
        descriptor = os.memfd_create(name, getattr(os, "MFD_CLOEXEC", 0))
    except AttributeError as exc:
        raise AttestationError("attestation verification requires memfd_create") from exc
    try:
        _write_all(descriptor, content)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _send_frame(connection: socket.socket, content: bytes) -> None:
    if len(content) > MAX_FRAME_BYTES:
        raise AttestationError("attestation frame is too large")
    packet = struct.pack("!I", len(content)) + content
    view = memoryview(packet)
    while view:
        try:
            sent = connection.send(view)
        except InterruptedError:
            continue
        if sent <= 0:
            raise AttestationError("attestation socket closed while sending")
        view = view[sent:]


def _send_frame_with_descriptor(connection: socket.socket, content: bytes, descriptor: int) -> None:
    if len(content) > MAX_FRAME_BYTES:
        raise AttestationError("attestation frame is too large")
    packet = struct.pack("!I", len(content)) + content
    try:
        sent = connection.sendmsg(
            [packet],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", descriptor))],
        )
    except (AttributeError, OSError) as exc:
        raise AttestationError("could not send runtime receipt descriptor") from exc
    if sent <= 0:
        raise AttestationError("attestation socket closed while sending")
    view = memoryview(packet)[sent:]
    while view:
        try:
            written = connection.send(view)
        except InterruptedError:
            continue
        if written <= 0:
            raise AttestationError("attestation socket closed while sending")
        view = view[written:]


def _receive_exact(connection: socket.socket, initial: bytes, length: int) -> bytes:
    chunks = [initial]
    remaining = length - len(initial)
    while remaining:
        try:
            chunk = connection.recv(remaining)
        except InterruptedError:
            continue
        if not chunk:
            raise AttestationError("attestation socket closed while receiving")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_frame(connection: socket.socket, *, max_bytes: int, require_descriptor: bool) -> tuple[bytes, tuple[int, ...]]:
    ancillary_size = socket.CMSG_SPACE(struct.calcsize("i") * 4) if require_descriptor else 0
    try:
        first, ancillary, flags, _address = connection.recvmsg(min(max_bytes + _FRAME_HEADER_BYTES, 65536), ancillary_size)
    except (AttributeError, OSError) as exc:
        raise AttestationError("could not receive attestation frame") from exc
    descriptors: list[int] = []
    try:
        if flags & getattr(socket, "MSG_CTRUNC", 0):
            raise AttestationError("attestation descriptor control data was truncated")
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS or len(data) % struct.calcsize("i"):
                raise AttestationError("attestation descriptor control data is invalid")
            descriptors.extend(
                struct.unpack(f"{len(data) // struct.calcsize('i')}i", data)
            )
        if len(first) < _FRAME_HEADER_BYTES:
            first = _receive_exact(connection, first, _FRAME_HEADER_BYTES)
            prefix = first
            payload_initial = b""
        else:
            prefix = first[:_FRAME_HEADER_BYTES]
            payload_initial = first[_FRAME_HEADER_BYTES:]
        length = struct.unpack("!I", prefix)[0]
        if length <= 0 or length > max_bytes:
            raise AttestationError("attestation frame length is invalid")
        if len(payload_initial) > length:
            raise AttestationError("attestation frame has trailing bytes")
        payload = _receive_exact(connection, payload_initial, length)
        if require_descriptor and len(descriptors) != 1:
            raise AttestationError("attestation request must carry exactly one receipt descriptor")
        if not require_descriptor and descriptors:
            raise AttestationError("attestation response must not carry descriptors")
        return payload, tuple(descriptors)
    except BaseException:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _read_record(records_descriptor: int, name: str, *, config: DaemonConfig) -> dict[str, object] | None:
    try:
        descriptor = os.open(name, _file_read_flags(), dir_fd=records_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AttestationError("attestation control record is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        identity = _file_identity(info)
        named = _file_identity(os.stat(name, dir_fd=records_descriptor, follow_symlinks=False))
        if (
            identity != named
            or identity[2] != RECORD_MODE
            or identity[3] != 1
            or int(info.st_uid) != config.root_uid
            or int(info.st_gid) != config.root_gid
            or identity[4] > config.max_frame_bytes
        ):
            raise AttestationError("attestation control record is unsafe")
        content = _read_bounded(descriptor, limit=config.max_frame_bytes, label="attestation control record")
        if _file_identity(os.fstat(descriptor)) != identity or _file_identity(
            os.stat(name, dir_fd=records_descriptor, follow_symlinks=False)
        ) != identity:
            raise AttestationError("attestation control record changed while reading")
    finally:
        os.close(descriptor)
    record = _strict_json_object(content, label="attestation control record")
    _validate_record(record, config=config)
    return record


def _validate_record(value: object, *, config: DaemonConfig) -> dict[str, object]:
    expected = {"schema", "created_at_ns", "peer", "request", "request_sha256", "response"}
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != RECORD_SCHEMA:
        raise AttestationError("attestation control record schema is invalid")
    _int(value.get("created_at_ns"), label="attestation control record creation time", minimum=1)
    peer = value.get("peer")
    if not isinstance(peer, dict) or set(peer) != {"uid", "gid"}:
        raise AttestationError("attestation control record peer is invalid")
    if _int(peer.get("uid"), label="attestation control record peer UID") != config.preparer.uid or _int(
        peer.get("gid"), label="attestation control record peer GID"
    ) != config.preparer.gid:
        raise AttestationError("attestation control record peer is invalid")
    request = validate_request(value.get("request"), config=config)
    request_sha = _lower_hex(value.get("request_sha256"), label="attestation control record request digest")
    if request_sha != hashlib.sha256(_canonical_json(request)).hexdigest():
        raise AttestationError("attestation control record request binding is invalid")
    response = validate_response(value.get("response"), request=request, config=config)
    return value


def _record_names(records_descriptor: int) -> tuple[str, ...]:
    try:
        names = tuple(os.listdir(f"/proc/self/fd/{records_descriptor}"))
    except OSError as exc:
        raise AttestationError("could not enumerate attestation records") from exc
    for name in names:
        if _RECORD_NAME.fullmatch(name) is None:
            raise AttestationError("attestation records directory contains an unexpected entry")
    return names


def _publish_record(
    records_descriptor: int,
    *,
    transaction_id: str,
    content: bytes,
    config: DaemonConfig,
) -> None:
    temporary = f".{transaction_id}.tmp.{os.getpid()}.{time.time_ns():x}"
    descriptor = -1
    try:
        descriptor = os.open(temporary, _file_write_flags(), RECORD_MODE, dir_fd=records_descriptor)
        os.fchmod(descriptor, RECORD_MODE)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        identity = _file_identity(info)
        if (
            identity[2] != RECORD_MODE
            or identity[3] != 1
            or int(info.st_uid) != config.root_uid
            or int(info.st_gid) != config.root_gid
            or identity[4] != len(content)
        ):
            raise AttestationError("staged attestation control record is unsafe")
        final_name = transaction_id + ".json"
        try:
            os.link(
                temporary,
                final_name,
                src_dir_fd=records_descriptor,
                dst_dir_fd=records_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise AttestationError("attestation transaction already has a control record") from exc
        os.fsync(records_descriptor)
        linked = _file_identity(os.stat(final_name, dir_fd=records_descriptor, follow_symlinks=False))
        if linked[:2] != identity[:2] or linked[2] != RECORD_MODE or linked[3] != 2:
            raise AttestationError("attestation control record changed during publication")
        os.unlink(temporary, dir_fd=records_descriptor)
        os.fsync(records_descriptor)
        final = _file_identity(os.stat(final_name, dir_fd=records_descriptor, follow_symlinks=False))
        if final[:2] != identity[:2] or final[2] != RECORD_MODE or final[3] != 1:
            raise AttestationError("attestation control record changed after publication")
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = -1
        try:
            temporary_info = os.stat(temporary, dir_fd=records_descriptor, follow_symlinks=False)
            if stat.S_ISREG(temporary_info.st_mode) and int(temporary_info.st_uid) == config.root_uid:
                os.unlink(temporary, dir_fd=records_descriptor)
                os.fsync(records_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class RuntimeAttestationDaemon:
    """A single-process root daemon with a deliberately small AF_UNIX protocol."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._listener: socket.socket | None = None
        self._bound_socket_identity: tuple[int, int] | None = None
        self._stopping = False

    def bind_listener(self) -> socket.socket:
        if self._listener is not None:
            return self._listener
        control_descriptor = _control_root_descriptor(self.config)
        try:
            name = self.config.socket_path.name
            try:
                existing = os.stat(name, dir_fd=control_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._remove_stale_socket(control_descriptor, name, existing)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(f"/proc/self/fd/{control_descriptor}/{name}")
                os.chown(name, self.config.root_uid, self.config.preparer.gid, dir_fd=control_descriptor, follow_symlinks=False)
                os.chmod(name, SOCKET_MODE, dir_fd=control_descriptor, follow_symlinks=False)
                info = os.stat(name, dir_fd=control_descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != SOCKET_MODE
                    or int(info.st_uid) != self.config.root_uid
                    or int(info.st_gid) != self.config.preparer.gid
                ):
                    raise AttestationError("attestation socket ownership or mode is unsafe")
                listener.listen(16)
                self._listener = listener
                self._bound_socket_identity = (int(info.st_dev), int(info.st_ino))
                return listener
            except BaseException:
                listener.close()
                raise
        finally:
            os.close(control_descriptor)

    def _remove_stale_socket(self, control_descriptor: int, name: str, info: os.stat_result) -> None:
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != SOCKET_MODE
            or int(info.st_uid) != self.config.root_uid
            or int(info.st_gid) != self.config.preparer.gid
        ):
            raise AttestationError("existing attestation socket is unsafe")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            try:
                probe.connect(f"/proc/self/fd/{control_descriptor}/{name}")
            except ConnectionRefusedError:
                pass
            except FileNotFoundError:
                return
            except socket.timeout as exc:
                raise AttestationError("existing attestation socket is live or ambiguous") from exc
            else:
                raise AttestationError("existing attestation socket is live")
        finally:
            probe.close()
        fresh = os.stat(name, dir_fd=control_descriptor, follow_symlinks=False)
        if (int(fresh.st_dev), int(fresh.st_ino)) != (int(info.st_dev), int(info.st_ino)):
            raise AttestationError("existing attestation socket changed during stale check")
        os.unlink(name, dir_fd=control_descriptor)
        os.fsync(control_descriptor)

    def close_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        identity, self._bound_socket_identity = self._bound_socket_identity, None
        if identity is None:
            return
        try:
            control_descriptor = _control_root_descriptor(self.config)
        except AttestationError:
            return
        try:
            try:
                current = os.stat(
                    self.config.socket_path.name,
                    dir_fd=control_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if (
                stat.S_ISSOCK(current.st_mode)
                and (int(current.st_dev), int(current.st_ino)) == identity
                and stat.S_IMODE(current.st_mode) == SOCKET_MODE
                and int(current.st_uid) == self.config.root_uid
                and int(current.st_gid) == self.config.preparer.gid
            ):
                os.unlink(self.config.socket_path.name, dir_fd=control_descriptor)
                os.fsync(control_descriptor)
        finally:
            os.close(control_descriptor)

    def stop(self) -> None:
        self._stopping = True
        self.close_listener()

    def attest(
        self,
        request: Mapping[str, object],
        *,
        receipt_descriptor: int,
        peer: PeerCredentials,
    ) -> dict[str, object]:
        """Process one request; exposed for focused deterministic tests."""

        _assert_peer(peer, config=self.config)
        normalized_request = validate_request(request, config=self.config)
        request_digest = hashlib.sha256(_canonical_json(normalized_request)).hexdigest()
        transaction_id, _path, _digest, _identity = _request_fields(normalized_request)
        control_descriptor = _control_root_descriptor(self.config)
        records_descriptor = -1
        try:
            records_descriptor = _records_descriptor(self.config, control_descriptor)
            existing = _read_record(records_descriptor, transaction_id + ".json", config=self.config)
            if existing is not None:
                if existing["request_sha256"] != request_digest:
                    raise AttestationError("attestation transaction collision")
                response = existing["response"]
                assert isinstance(response, dict)
                return response
            if len(_record_names(records_descriptor)) >= self.config.max_records:
                raise AttestationError("attestation record capacity is exhausted")
            _content, observed_identity = _read_runtime_receipt(
                self.config,
                normalized_request,
                receipt_descriptor,
            )
            private_descriptor = _open_private_key(self.config, control_descriptor)
            public_descriptor = -1
            try:
                public_descriptor = _open_public_key(self.config)
                private_der = _public_der_from_private(self.config, private_descriptor)
                public_der = _public_der_from_public(self.config, public_descriptor)
                if private_der != public_der:
                    raise AttestationError("attestation private and public keys do not match")
                issued = time.time_ns()
                response: dict[str, object] = {
                    "schema": RESPONSE_SCHEMA,
                    "transaction_id": transaction_id,
                    "receipt_path": str(self.config.runtime_receipt_path),
                    "receipt_core_sha256": _request_fields(normalized_request)[2],
                    "receipt_core_identity": list(observed_identity),
                    "request_sha256": request_digest,
                    "issued_at_ns": issued,
                    "attestation": {
                        "algorithm": SIGNATURE_ALGORITHM,
                        "key_id": hashlib.sha256(public_der).hexdigest(),
                        "signature": "",
                    },
                }
                signature = _sign_payload(self.config, private_descriptor, _signature_payload(response))
                response["attestation"] = {
                    "algorithm": SIGNATURE_ALGORITHM,
                    "key_id": hashlib.sha256(public_der).hexdigest(),
                    "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
                }
                validate_response(response, request=normalized_request, config=self.config)
            finally:
                if public_descriptor >= 0:
                    os.close(public_descriptor)
                os.close(private_descriptor)
            record = {
                "schema": RECORD_SCHEMA,
                "created_at_ns": time.time_ns(),
                "peer": {"uid": peer.uid, "gid": peer.gid},
                "request": normalized_request,
                "request_sha256": request_digest,
                "response": response,
            }
            _publish_record(
                records_descriptor,
                transaction_id=transaction_id,
                content=_canonical_json(record),
                config=self.config,
            )
            return response
        finally:
            if records_descriptor >= 0:
                os.close(records_descriptor)
            os.close(control_descriptor)

    def serve_listener(self, listener: socket.socket | None = None) -> None:
        active = listener or self.bind_listener()
        accepted = 0
        active.settimeout(1.0)
        while not self._stopping and accepted < self.config.max_connections:
            try:
                connection, _ = active.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping:
                    break
                raise
            accepted += 1
            with connection:
                connection.settimeout(self.config.connection_timeout)
                received: tuple[int, ...] = ()
                try:
                    peer = _peer_credentials(connection)
                    payload, received = _receive_frame(
                        connection,
                        max_bytes=self.config.max_frame_bytes,
                        require_descriptor=True,
                    )
                    request = _strict_json_object(payload, label="attestation request")
                    if payload != _canonical_json(request):
                        raise AttestationError("attestation request is not canonical")
                    response = self.attest(request, receipt_descriptor=received[0], peer=peer)
                    _send_frame(connection, _canonical_json(response))
                except AttestationError:
                    # A generic, bounded failure avoids turning the root broker
                    # into an oracle for filesystem or key state.
                    try:
                        _send_frame(connection, _canonical_json({"schema": ERROR_SCHEMA, "ok": False}))
                    except AttestationError:
                        pass
                finally:
                    for descriptor in received:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass


def request_runtime_attestation(
    request: Mapping[str, object],
    *,
    receipt_descriptor: int,
    config: DaemonConfig,
    timeout: float | None = None,
) -> dict[str, object]:
    """Ask the root broker to attest the exact descriptor in ``request``.

    This client intentionally rejects execution as ``max`` before connecting.
    A caller should publish the returned detached response alongside the
    receipt and verify it with :func:`verify_response_signature` later.
    """

    _assert_client_principal(config)
    normalized = validate_request(request, config=config)
    _validate_socket_for_client(config)
    deadline = config.connection_timeout if timeout is None else timeout
    if deadline <= 0 or deadline > 60:
        raise AttestationError("attestation client timeout is unsafe")
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(deadline)
        connection.connect(str(config.socket_path))
    except OSError as exc:
        raise AttestationError("could not connect to the root attestation broker") from exc
    with connection:
        _send_frame_with_descriptor(connection, _canonical_json(normalized), receipt_descriptor)
        payload, descriptors = _receive_frame(connection, max_bytes=config.max_frame_bytes, require_descriptor=False)
        del descriptors
    response = _strict_json_object(payload, label="attestation response")
    if response.get("schema") == ERROR_SCHEMA:
        raise AttestationError("root attestation broker rejected the request")
    if payload != _canonical_json(response):
        raise AttestationError("attestation response is not canonical")
    return validate_response(response, request=normalized, config=config)


def _validate_socket_for_client(config: DaemonConfig) -> None:
    control_descriptor = _control_root_descriptor(config, traversal_only=True)
    try:
        info = os.stat(
            config.socket_path.name,
            dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != SOCKET_MODE
            or int(info.st_uid) != config.root_uid
            or int(info.st_gid) != config.preparer.gid
        ):
            raise AttestationError("attestation socket is unsafe")
    except OSError as exc:
        raise AttestationError("attestation socket is unavailable") from exc
    finally:
        os.close(control_descriptor)


def generate_keypair(config: DaemonConfig) -> None:
    """Explicitly generate an Ed25519 keypair; daemon startup never self-heals keys."""

    if os.geteuid() != config.root_uid:
        raise AttestationError("only the root attestor may initialize a signing key")
    control_descriptor = _control_root_descriptor(config)
    public_parent = -1
    private_temporary: str | None = None
    public_temporary: str | None = None
    private_descriptor = -1
    public_descriptor = -1
    try:
        public_parent = _open_absolute_directory(config.public_key_path.parent, label="attestation public key parent")
        public_parent_info = os.fstat(public_parent)
        if (
            not stat.S_ISDIR(public_parent_info.st_mode)
            or int(public_parent_info.st_uid) != config.root_uid
            or stat.S_IMODE(public_parent_info.st_mode) & 0o022
        ):
            raise AttestationError("attestation public key parent is unsafe")
        for descriptor, name in ((control_descriptor, "runtime-attestd.key"), (public_parent, config.public_key_path.name)):
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise AttestationError("attestation key material already exists")
        nonce = f"{os.getpid()}.{time.time_ns():x}"
        private_temporary = ".runtime-attestd.key.tmp." + nonce
        public_temporary = ".runtime-attestd.pub.tmp." + nonce
        private_descriptor = os.open(
            private_temporary,
            _file_read_write_create_flags(),
            PRIVATE_KEY_MODE,
            dir_fd=control_descriptor,
        )
        os.fchmod(private_descriptor, PRIVATE_KEY_MODE)
        try:
            result = subprocess.run(
                [os.fspath(config.openssl_path), "genpkey", "-algorithm", "ED25519"],
                stdin=subprocess.DEVNULL,
                stdout=private_descriptor,
                stderr=subprocess.PIPE,
                check=False,
                close_fds=True,
                timeout=8,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AttestationError("could not generate attestation key material") from exc
        if result.returncode != 0:
            raise AttestationError("could not generate attestation key material")
        os.fsync(private_descriptor)
        os.lseek(private_descriptor, 0, os.SEEK_SET)
        private_der = _public_der_from_private(config, private_descriptor)
        os.lseek(private_descriptor, 0, os.SEEK_SET)
        public_pem = _openssl(
            config,
            ["pkey", "-in", f"/proc/self/fd/{private_descriptor}", "-pubout"],
            pass_fds=(private_descriptor,),
        )
        public_descriptor = os.open(
            public_temporary,
            _file_read_write_create_flags(),
            PUBLIC_KEY_MODE,
            dir_fd=public_parent,
        )
        os.fchmod(public_descriptor, PUBLIC_KEY_MODE)
        _write_all(public_descriptor, public_pem)
        os.fsync(public_descriptor)
        os.lseek(public_descriptor, 0, os.SEEK_SET)
        public_der = _public_der_from_public(config, public_descriptor)
        if private_der != public_der:
            raise AttestationError("generated attestation keypair is inconsistent")
        _link_noreplace(control_descriptor, private_temporary, "runtime-attestd.key")
        _link_noreplace(public_parent, public_temporary, config.public_key_path.name)
        os.fsync(control_descriptor)
        os.fsync(public_parent)
        os.unlink(private_temporary, dir_fd=control_descriptor)
        private_temporary = None
        os.unlink(public_temporary, dir_fd=public_parent)
        public_temporary = None
        os.fsync(control_descriptor)
        os.fsync(public_parent)
        # Re-open through the normal hardened path before reporting success.
        key = _open_private_key(config, control_descriptor)
        public = _open_public_key(config)
        try:
            if _public_der_from_private(config, key) != _public_der_from_public(config, public):
                raise AttestationError("published attestation keypair is inconsistent")
        finally:
            os.close(key)
            os.close(public)
    finally:
        if private_descriptor >= 0:
            os.close(private_descriptor)
        if public_descriptor >= 0:
            os.close(public_descriptor)
        if private_temporary is not None:
            try:
                os.unlink(private_temporary, dir_fd=control_descriptor)
            except OSError:
                pass
        if public_temporary is not None and public_parent >= 0:
            try:
                os.unlink(public_temporary, dir_fd=public_parent)
            except OSError:
                pass
        if public_parent >= 0:
            os.close(public_parent)
        os.close(control_descriptor)


def _link_noreplace(directory: int, source: str, destination: str) -> None:
    try:
        os.link(source, destination, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except FileExistsError as exc:
        raise AttestationError("attestation key material appeared during publication") from exc


def _daemon_signal_handler(daemon: RuntimeAttestationDaemon, _signum: int, _frame: object) -> None:
    daemon.stop()


def _cli_config(arguments: argparse.Namespace) -> DaemonConfig:
    base = production_config()
    return DaemonConfig(
        control_root=Path(arguments.control_root),
        socket_path=Path(arguments.socket_path),
        records_path=Path(arguments.records_path),
        private_key_path=Path(arguments.private_key_path),
        public_key_path=Path(arguments.public_key_path),
        runtime_receipt_path=Path(arguments.runtime_receipt_path),
        preparer=base.preparer,
        root_uid=base.root_uid,
        root_gid=base.root_gid,
        max_uid=base.max_uid,
        openssl_path=Path(arguments.openssl_path),
        max_records=arguments.max_records,
        max_connections=arguments.max_connections,
    )


def _run_client(arguments: argparse.Namespace) -> int:
    """Run the narrow preparer-side client against the production endpoint."""

    transaction_id = _transaction_id(arguments.transaction_id)
    config = production_config()
    parent, _name, descriptor = _open_parent_and_leaf(
        config.runtime_receipt_path,
        label="staged runtime receipt",
        flags=_file_read_flags(),
    )
    try:
        request = build_request(
            transaction_id=transaction_id,
            receipt_path=config.runtime_receipt_path,
            receipt_descriptor=descriptor,
            config=config,
        )
        response = request_runtime_attestation(
            request,
            receipt_descriptor=descriptor,
            config=config,
        )
    finally:
        os.close(descriptor)
        os.close(parent)
    sys.stdout.buffer.write(_canonical_json(response))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Root-controlled TermDeck runtime attestor")
    parser.add_argument("command", choices=("daemon", "keygen", "client"))
    parser.add_argument("--control-root", default=str(DEFAULT_CONTROL_ROOT))
    parser.add_argument("--socket-path", default=str(DEFAULT_SOCKET_PATH))
    parser.add_argument("--records-path", default=str(DEFAULT_RECORDS_PATH))
    parser.add_argument("--private-key-path", default=str(DEFAULT_PRIVATE_KEY_PATH))
    parser.add_argument("--public-key-path", default=str(DEFAULT_PUBLIC_KEY_PATH))
    parser.add_argument("--runtime-receipt-path", default=str(DEFAULT_RUNTIME_RECEIPT_PATH))
    parser.add_argument("--openssl-path", default=str(DEFAULT_OPENSSL_PATH))
    parser.add_argument("--max-records", type=int, default=MAX_RECORDS)
    parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS)
    parser.add_argument("--transaction-id")
    arguments = parser.parse_args(argv)
    if arguments.command in {"daemon", "keygen"} and os.geteuid() != 0:
        raise SystemExit("termdeck runtime attestor must run as root")
    try:
        if arguments.command == "client":
            if arguments.transaction_id is None:
                raise AttestationError("attestation client requires a transaction id")
            return _run_client(arguments)
        config = _cli_config(arguments)
        if arguments.command == "keygen":
            generate_keypair(config)
            return 0
        daemon = RuntimeAttestationDaemon(config)
        signal.signal(signal.SIGTERM, lambda signum, frame: _daemon_signal_handler(daemon, signum, frame))
        signal.signal(signal.SIGINT, lambda signum, frame: _daemon_signal_handler(daemon, signum, frame))
        try:
            daemon.serve_listener()
        finally:
            daemon.close_listener()
        return 0
    except AttestationError as exc:
        raise SystemExit(f"TermDeck runtime attestor refused: {exc}") from exc


if __name__ == "__main__":  # pragma: no cover - exercised through the installed unit
    raise SystemExit(main())


__all__ = [
    "AttestationError",
    "CONTROL_DIRECTORY_MODE",
    "DEFAULT_CONTROL_ROOT",
    "DEFAULT_PRIVATE_KEY_PATH",
    "DEFAULT_PUBLIC_KEY_PATH",
    "DEFAULT_RECORDS_PATH",
    "DEFAULT_RUNTIME_RECEIPT_PATH",
    "DEFAULT_SOCKET_PATH",
    "DaemonConfig",
    "PeerCredentials",
    "Principal",
    "RECORD_SCHEMA",
    "RESPONSE_SCHEMA",
    "REQUEST_SCHEMA",
    "RuntimeAttestationDaemon",
    "SIGNATURE_ALGORITHM",
    "VerifiedRuntimeReceiptLease",
    "build_request",
    "generate_keypair",
    "open_verified_runtime_receipt",
    "production_config",
    "request_runtime_attestation",
    "validate_request",
    "validate_response",
    "validate_runtime_build_receipt",
    "validate_runtime_build_core",
    "verify_runtime_build_attestation",
    "verify_response_signature",
]
