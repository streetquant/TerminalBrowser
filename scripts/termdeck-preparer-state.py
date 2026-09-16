#!/usr/bin/python3
"""Finalize or verify a TermDeck runtime receipt on its published inode.

The runtime core publishes ``build-receipt.json`` before asking the root
attestation broker to sign it.  This helper retains both the receipt descriptor
and its parent directory descriptor for the whole transaction.  The broker sees
that exact receipt descriptor, and the returned Ed25519 response is written back
through the same descriptor only after the named path and inode still match.
The read-only verifier authenticates historical receipt bytes without treating
the current runtime sources as part of that verification transaction.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from termdeck import runtime_attestation


RECEIPT_MODE = 0o644
RECEIPT_JOURNAL_MODE = 0o600
_RECEIPT_JOURNAL_PREFIX = ".termdeck-receipt-transaction-"
_RECEIPT_JOURNAL_NAME = re.compile(
    re.escape(_RECEIPT_JOURNAL_PREFIX) + r"[0-9a-f]{32}\Z"
)
_RECEIPT_JOURNAL_DISPOSAL_NAME = re.compile(
    re.escape(_RECEIPT_JOURNAL_PREFIX)
    + r"[0-9a-f]{32}-dispose-[0-9]+-[0-9]+-[0-9]+--?[0-9]+\.json\Z"
)
_RENAME_NOREPLACE = 1
_MAX_JOURNAL_BYTES = 256 * 1024
_TLS_JOURNAL_NAME = re.compile(r"\.termdeck-tls-transaction-[0-9a-f]{32}\Z")
_HISTORICAL_RECEIPT_NAMES = frozenset(
    {"prior-build-receipt.json", "rollback-prior-build-receipt.json"}
)


class ReceiptStateError(RuntimeError):
    """The receipt could not safely advance to the next publication state."""


class ReceiptState(Enum):
    NEW = auto()
    CORE_OPEN = auto()
    ATTESTED = auto()
    FINALIZED = auto()
    CLOSED = auto()


ReceiptIdentity = tuple[int, int, int, int, int, int, int]
DirectoryIdentity = tuple[int, int]
Signer = Callable[
    [Mapping[str, object], int, runtime_attestation.DaemonConfig],
    Mapping[str, object],
]


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
        raise ReceiptStateError("runtime receipt is not canonicalizable") from exc


def _strict_json_object(content: bytes) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ReceiptStateError("runtime receipt has a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ReceiptStateError("runtime receipt contains a non-finite JSON value")

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReceiptStateError("runtime receipt is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptStateError("runtime receipt must be a JSON object")
    return value


def _identity(info: os.stat_result) -> ReceiptIdentity:
    if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise ReceiptStateError("runtime receipt is not a regular file")
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IMODE(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _identity_from_value(value: object, *, label: str) -> ReceiptIdentity:
    if (
        not isinstance(value, list)
        or len(value) != 7
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ReceiptStateError(f"{label} is invalid")
    parsed = tuple(value)
    if parsed[0] < 0 or parsed[1] <= 0 or parsed[2] < 0 or parsed[3] != 1 or parsed[4] < 0:
        raise ReceiptStateError(f"{label} is invalid")
    return parsed  # type: ignore[return-value]


def _directory_identity(info: os.stat_result) -> DirectoryIdentity:
    if not stat.S_ISDIR(info.st_mode) or info.st_ino <= 0 or info.st_nlink <= 0:
        raise ReceiptStateError("runtime receipt parent is not a directory")
    return int(info.st_dev), int(info.st_ino)


def _absolute_canonical(path: str | os.PathLike[str]) -> Path:
    raw = os.fsdecode(os.fspath(path))
    if (
        not raw
        or not os.path.isabs(raw)
        or raw != os.path.normpath(raw)
        or raw == "/"
        or "\x00" in raw
    ):
        raise ReceiptStateError("runtime receipt path must be absolute and canonical")
    result = Path(raw)
    if any(part in {"", ".", ".."} for part in result.parts[1:]):
        raise ReceiptStateError("runtime receipt path must be absolute and canonical")
    return result


def _verification_path(
    receipt_path: str | os.PathLike[str],
    config: runtime_attestation.DaemonConfig,
) -> Path:
    """Allow only the public receipt or its exact TLS-journal prior location."""

    path = _absolute_canonical(receipt_path)
    canonical = _absolute_canonical(config.runtime_receipt_path)
    if path == canonical:
        return path
    if (
        path.name not in _HISTORICAL_RECEIPT_NAMES
        or path.parent.parent != canonical.parent
        or _TLS_JOURNAL_NAME.fullmatch(path.parent.name) is None
    ):
        raise ReceiptStateError(
            "runtime receipt verification path is not canonical or journal-held"
        )
    return path


def _directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow:
        raise ReceiptStateError("receipt finalization requires O_DIRECTORY and O_NOFOLLOW")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _receipt_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReceiptStateError("receipt finalization requires O_NOFOLLOW")
    return os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)


def _receipt_read_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReceiptStateError("receipt verification requires O_NOFOLLOW")
    return os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)


def _receipt_journal_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReceiptStateError("receipt journaling requires O_NOFOLLOW")
    return os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)


def _receipt_journal_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReceiptStateError("receipt journaling requires O_NOFOLLOW")
    return os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_parent(path: Path) -> int:
    flags = _directory_flags()
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or (int(opened.st_dev), int(opened.st_ino))
                    != (int(named.st_dev), int(named.st_ino))
                ):
                    raise ReceiptStateError("runtime receipt parent changed while opening")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_preparer_parent(
    descriptor: int,
    config: runtime_attestation.DaemonConfig,
    *,
    label: str,
) -> DirectoryIdentity:
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise ReceiptStateError(f"{label} is unavailable") from exc
    identity = _directory_identity(info)
    if (
        stat.S_IMODE(info.st_mode) != 0o700
        or int(info.st_uid) != config.preparer.uid
        or int(info.st_gid) != config.preparer.gid
    ):
        raise ReceiptStateError(f"{label} ownership or mode is unsafe")
    return identity


def _journal_name(transaction_id: str) -> str:
    if not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise ReceiptStateError("receipt journal transaction id is invalid")
    return _RECEIPT_JOURNAL_PREFIX + transaction_id


def _journal_disposal_name(name: str, identity: ReceiptIdentity) -> str:
    if _RECEIPT_JOURNAL_NAME.fullmatch(name) is None:
        raise ReceiptStateError("receipt journal name is invalid")
    stem = name[: -len(".json")]
    return (
        f"{stem}-dispose-{identity[0]}-{identity[1]}-{identity[4]}-{identity[5]}.json"
    )


def _journal_name_is_valid(name: str) -> bool:
    return bool(
        _RECEIPT_JOURNAL_NAME.fullmatch(name)
        or _RECEIPT_JOURNAL_DISPOSAL_NAME.fullmatch(name)
    )


def _rename_noreplace(parent: int, source: str, destination: str) -> None:
    """Move one checked leaf without replacing a concurrently-created name."""

    if not source or not destination or "/" in source or "/" in destination:
        raise ReceiptStateError("receipt journal disposal name is invalid")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise ReceiptStateError("receipt journal cleanup requires renameat2") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    for _attempt in range(8):
        if renameat2(
            parent,
            os.fsencode(source),
            parent,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        ) == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EINTR:
            continue
        if error == errno.ENOENT:
            raise FileNotFoundError(error, os.strerror(error), source)
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        raise OSError(error, os.strerror(error), source, destination)
    raise ReceiptStateError("receipt journal cleanup was interrupted")


def _journal_payload(
    *,
    state: str,
    identity: ReceiptIdentity,
    core: bytes | None,
    final_identity: ReceiptIdentity | None = None,
) -> bytes:
    if state not in {"prepared", "committed"}:
        raise ReceiptStateError("receipt journal state is invalid")
    value = {
        "core": None if core is None else base64.b64encode(core).decode("ascii"),
        "identity": list(identity),
        "final_identity": None if final_identity is None else list(final_identity),
        "schema": "termdeck.receipt-finalization-journal.v1",
        "state": state,
    }
    return _canonical_json(value)


def _open_bound_journal(
    parent: int,
    name: str,
    config: runtime_attestation.DaemonConfig,
) -> tuple[int, ReceiptIdentity]:
    if not _journal_name_is_valid(name):
        raise ReceiptStateError("receipt journal name is invalid")
    try:
        named_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        named_identity = _identity(named_before)
    except (OSError, ReceiptStateError) as exc:
        raise ReceiptStateError("could not inspect receipt journal") from exc
    if (
        int(named_before.st_uid) != config.preparer.uid
        or int(named_before.st_gid) != config.preparer.gid
        or named_identity[3] != 1
        or named_identity[2] & 0o077
    ):
        raise ReceiptStateError("receipt journal ownership or mode is unsafe")
    # A crash can leave an O_EXCL inode with a mode narrowed by umask before
    # the producer's fchmod. Normalize that authenticated same-owner inode so
    # recovery can read it, including the 0000 case.
    if named_identity[2] != RECEIPT_JOURNAL_MODE:
        try:
            os.chmod(
                name,
                RECEIPT_JOURNAL_MODE,
                dir_fd=parent,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ReceiptStateError("receipt journal mode could not be normalized") from exc
    try:
        descriptor = os.open(name, _receipt_journal_open_flags(), dir_fd=parent)
    except OSError as exc:
        raise ReceiptStateError("could not open receipt journal") from exc
    try:
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = _identity(info)
        named_identity = _identity(named)
        if (
            identity != named_identity
            or identity[2] != RECEIPT_JOURNAL_MODE
            or identity[3] != 1
            or int(info.st_uid) != config.preparer.uid
            or int(info.st_gid) != config.preparer.gid
            or identity[4] > _MAX_JOURNAL_BYTES
        ):
            raise ReceiptStateError("receipt journal ownership, mode, or identity is unsafe")
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _remove_bound_journal(parent: int, name: str, descriptor: int) -> None:
    try:
        current = _identity(os.fstat(descriptor))
        named = _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except (OSError, ReceiptStateError) as exc:
        raise ReceiptStateError("receipt journal changed before removal") from exc
    if current != named:
        raise ReceiptStateError("receipt journal path was replaced")
    try:
        if _RECEIPT_JOURNAL_DISPOSAL_NAME.fullmatch(name):
            disposal = name
        else:
            disposal = _journal_disposal_name(name, current)
            _rename_noreplace(parent, name, disposal)
            moved = _identity(os.fstat(descriptor))
            named_moved = _identity(
                os.stat(disposal, dir_fd=parent, follow_symlinks=False)
            )
            if moved[:5] != current[:5] or named_moved[:5] != current[:5]:
                raise ReceiptStateError("receipt journal changed during disposal")
        os.unlink(disposal, dir_fd=parent)
        os.fsync(parent)
    except OSError as exc:
        raise ReceiptStateError("receipt journal could not be removed") from exc


def _recover_receipt_journals(
    receipt_path: Path,
    config: runtime_attestation.DaemonConfig,
) -> None:
    """Recover every descriptor-bound finalization journal before new signing."""

    path = _absolute_canonical(receipt_path)
    configured = _absolute_canonical(config.runtime_receipt_path)
    if path != configured:
        raise ReceiptStateError("runtime receipt path does not match attestor configuration")
    parent = _open_parent(path.parent)
    try:
        _assert_preparer_parent(parent, config, label="runtime receipt parent")
        try:
            names = tuple(
                name
                for name in os.listdir(f"/proc/self/fd/{parent}")
                if name.startswith(_RECEIPT_JOURNAL_PREFIX)
            )
        except OSError as exc:
            raise ReceiptStateError("could not enumerate receipt journals") from exc
        for name in sorted(names):
            descriptor, _journal_identity = _open_bound_journal(parent, name, config)
            try:
                if _RECEIPT_JOURNAL_DISPOSAL_NAME.fullmatch(name):
                    # A crash may land after the descriptor-bound no-replace
                    # move and before the final unlink.  The disposal name
                    # itself is inode-bound by _open_bound_journal; consume
                    # only that exact private leaf.
                    _remove_bound_journal(parent, name, descriptor)
                    continue
                content = _read_all(descriptor, limit=_MAX_JOURNAL_BYTES)
                if not content:
                    # O_EXCL creation may be the last durable event before a
                    # crash.  The public inode was not touched yet; consume
                    # this exact empty journal rather than leaving a fixed,
                    # unrecoverable name behind.
                    _remove_bound_journal(parent, name, descriptor)
                    continue
                value = _strict_json_object(content)
                if content != _canonical_json(value):
                    raise ReceiptStateError("receipt journal is not canonical JSON")
                if set(value) != {"core", "final_identity", "identity", "schema", "state"} or value.get("schema") != "termdeck.receipt-finalization-journal.v1":
                    raise ReceiptStateError("receipt journal schema is invalid")
                raw_identity = value.get("identity")
                expected = _identity_from_value(raw_identity, label="receipt journal identity")
                state = value.get("state")
                if state == "committed":
                    final_identity = value.get("final_identity")
                    final = _identity_from_value(
                        final_identity,
                        label="receipt journal final identity",
                    )
                    try:
                        current = _identity(
                            os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                        )
                    except (OSError, ReceiptStateError) as exc:
                        raise ReceiptStateError("public receipt disappeared during recovery") from exc
                    if current != final:
                        raise ReceiptStateError("public receipt changed before committed journal recovery")
                    _remove_bound_journal(parent, name, descriptor)
                    continue
                if state != "prepared":
                    raise ReceiptStateError("receipt journal state is invalid")
                encoded = value.get("core")
                if not isinstance(encoded, str):
                    raise ReceiptStateError("receipt journal core is invalid")
                try:
                    core = base64.b64decode(encoded, validate=True)
                except (ValueError, UnicodeError) as exc:
                    raise ReceiptStateError("receipt journal core is invalid") from exc
                if not core or len(core) > config.max_receipt_bytes:
                    raise ReceiptStateError("receipt journal core is invalid")
                try:
                    current = _identity(
                        os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    )
                except (OSError, ReceiptStateError) as exc:
                    raise ReceiptStateError("public receipt disappeared during recovery") from exc
                if current != expected:
                    raise ReceiptStateError("public receipt identity changed before recovery")
                receipt_descriptor = os.open(path.name, _receipt_flags(), dir_fd=parent)
                try:
                    # The second descriptor is opened only after identity
                    # preflight; the public name remains bound by the same
                    # parent anchor and is checked again before committing.
                    _assert_opened_receipt(
                        parent_descriptor=parent,
                        parent_identity=_directory_identity(os.fstat(parent)),
                        receipt_descriptor=receipt_descriptor,
                        receipt_path=path,
                        expected=expected,
                    )
                    if _identity(os.fstat(receipt_descriptor)) != expected:
                        raise ReceiptStateError("public receipt changed before recovery")
                    receipt_info = os.fstat(receipt_descriptor)
                    if (
                        int(receipt_info.st_uid) != config.preparer.uid
                        or int(receipt_info.st_gid) != config.preparer.gid
                    ):
                        raise ReceiptStateError("public receipt ownership changed before recovery")
                    os.ftruncate(receipt_descriptor, 0)
                    os.lseek(receipt_descriptor, 0, os.SEEK_SET)
                    _write_all(receipt_descriptor, core)
                    os.fsync(receipt_descriptor)
                    _assert_opened_receipt(
                        parent_descriptor=parent,
                        parent_identity=_directory_identity(os.fstat(parent)),
                        receipt_descriptor=receipt_descriptor,
                        receipt_path=path,
                        expected=_identity(os.fstat(receipt_descriptor)),
                    )
                    _assert_preparer_parent(
                        parent,
                        config,
                        label="runtime receipt parent after recovery",
                    )
                finally:
                    os.close(receipt_descriptor)
                _remove_bound_journal(parent, name, descriptor)
            finally:
                os.close(descriptor)
    finally:
        os.close(parent)


def _read_all(descriptor: int, *, limit: int) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise ReceiptStateError("runtime receipt descriptor is not seekable") from exc
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
    content = b"".join(chunks)
    if len(content) > limit:
        raise ReceiptStateError("runtime receipt exceeds its bounded size")
    return content


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise ReceiptStateError("runtime receipt finalization write failed")
        view = view[written:]


def _write_journal_all(descriptor: int, content: bytes) -> None:
    """Write a journal frame without sharing the receipt fault boundary."""

    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise ReceiptStateError("receipt journal write failed")
        view = view[written:]


def _assert_canonical_parent(
    descriptor: int,
    expected: DirectoryIdentity,
    path: Path,
) -> None:
    reopened = -1
    try:
        retained = _directory_identity(os.fstat(descriptor))
        reopened = _open_parent(path)
        named = _directory_identity(os.fstat(reopened))
    except (OSError, ReceiptStateError) as exc:
        raise ReceiptStateError("runtime receipt parent path was replaced") from exc
    finally:
        if reopened >= 0:
            os.close(reopened)
    if retained != expected or named != expected:
        raise ReceiptStateError("runtime receipt parent path was replaced")


def _assert_opened_receipt(
    *,
    parent_descriptor: int,
    parent_identity: DirectoryIdentity,
    receipt_descriptor: int,
    receipt_path: Path,
    expected: ReceiptIdentity,
) -> None:
    _assert_canonical_parent(parent_descriptor, parent_identity, receipt_path.parent)
    try:
        opened = _identity(os.fstat(receipt_descriptor))
        named = _identity(
            os.stat(
                receipt_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
    except (OSError, ReceiptStateError) as exc:
        raise ReceiptStateError(
            "runtime receipt path is no longer bound to its descriptor"
        ) from exc
    if opened != expected or named != expected:
        raise ReceiptStateError("runtime receipt changed or its path was replaced")
    if opened[2] != RECEIPT_MODE or opened[3] != 1:
        raise ReceiptStateError("runtime receipt mode or link count is unsafe")


def _default_signer(
    request: Mapping[str, object],
    receipt_descriptor: int,
    config: runtime_attestation.DaemonConfig,
) -> Mapping[str, object]:
    return runtime_attestation.request_runtime_attestation(
        request,
        receipt_descriptor=receipt_descriptor,
        config=config,
    )


@dataclass
class ReceiptFinalizer:
    """Descriptor-held state machine for unsigned-to-attested publication."""

    receipt_path: Path
    transaction_id: str
    config: runtime_attestation.DaemonConfig
    signer: Signer = _default_signer
    state: ReceiptState = ReceiptState.NEW
    _parent_descriptor: int = -1
    _parent_identity: DirectoryIdentity | None = None
    _receipt_descriptor: int = -1
    _core_identity: ReceiptIdentity | None = None
    _core: dict[str, object] | None = None
    _request: dict[str, object] | None = None
    _response: dict[str, object] | None = None
    _core_bytes: bytes | None = None
    _journal_name: str | None = None
    _journal_descriptor: int = -1
    _journal_identity: ReceiptIdentity | None = None

    def __post_init__(self) -> None:
        self.receipt_path = _absolute_canonical(self.receipt_path)
        configured = _absolute_canonical(self.config.runtime_receipt_path)
        if self.receipt_path != configured:
            raise ReceiptStateError("runtime receipt path does not match attestor configuration")

    def __enter__(self) -> "ReceiptFinalizer":
        return self.open_core()

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.close()

    def _require_state(self, expected: ReceiptState) -> None:
        if self.state is not expected:
            raise ReceiptStateError(
                "runtime receipt state is "
                f"{self.state.name.lower()}, expected {expected.name.lower()}"
            )

    def _observed_identities(self) -> tuple[ReceiptIdentity, ReceiptIdentity]:
        self._assert_parent_bound()
        try:
            opened = _identity(os.fstat(self._receipt_descriptor))
            named = _identity(
                os.stat(
                    self.receipt_path.name,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            )
        except (OSError, ReceiptStateError) as exc:
            raise ReceiptStateError(
                "runtime receipt path is no longer bound to its descriptor"
            ) from exc
        return opened, named

    def _assert_parent_bound(self) -> None:
        expected = self._parent_identity
        if expected is None:
            raise ReceiptStateError("runtime receipt parent identity is unavailable")
        _assert_canonical_parent(
            self._parent_descriptor,
            expected,
            self.receipt_path.parent,
        )
        _assert_preparer_parent(
            self._parent_descriptor,
            self.config,
            label="runtime receipt parent",
        )

    def _assert_core_bound(self) -> None:
        expected = self._core_identity
        if expected is None:
            raise ReceiptStateError("runtime receipt core identity is unavailable")
        opened, named = self._observed_identities()
        if opened != expected or named != expected:
            raise ReceiptStateError("runtime receipt core changed or its path was replaced")
        if opened[2] != RECEIPT_MODE or opened[3] != 1:
            raise ReceiptStateError("runtime receipt mode or link count changed")

    def _assert_final_bound(self, expected: ReceiptIdentity) -> None:
        opened, named = self._observed_identities()
        core_identity = self._core_identity
        if core_identity is None:
            raise ReceiptStateError("runtime receipt core identity is unavailable")
        if opened != expected or named != expected:
            raise ReceiptStateError("final runtime receipt changed or its path was replaced")
        if opened[:4] != core_identity[:4]:
            raise ReceiptStateError("final runtime receipt is not the published core inode")
        if opened[2] != RECEIPT_MODE or opened[3] != 1:
            raise ReceiptStateError("final runtime receipt mode or link count is unsafe")

    def open_core(self) -> "ReceiptFinalizer":
        self._require_state(ReceiptState.NEW)
        parent = _open_parent(self.receipt_path.parent)
        try:
            descriptor = os.open(
                self.receipt_path.name,
                _receipt_flags(),
                dir_fd=parent,
            )
        except BaseException:
            os.close(parent)
            raise
        self._parent_descriptor = parent
        self._receipt_descriptor = descriptor
        try:
            self._parent_identity = _directory_identity(os.fstat(parent))
            opened, named = self._observed_identities()
            if opened != named:
                raise ReceiptStateError("runtime receipt path does not name its opened descriptor")
            if opened[2] != RECEIPT_MODE or opened[3] != 1:
                raise ReceiptStateError("runtime receipt must have mode 0644 and one link")
            self._core_identity = opened
            content = _read_all(descriptor, limit=self.config.max_receipt_bytes)
            self._assert_core_bound()
            core = _strict_json_object(content)
            if "producer_commitment" in core:
                raise ReceiptStateError("HMAC producer_commitment receipts are forbidden")
            if "producer_attestation" in core:
                raise ReceiptStateError("runtime receipt is already finalized")
            if content != _canonical_json(core):
                raise ReceiptStateError("runtime receipt core is not canonical JSON")
            try:
                runtime_attestation.validate_runtime_build_core(core)
            except runtime_attestation.AttestationError as exc:
                raise ReceiptStateError("runtime receipt core is invalid") from exc
            request = runtime_attestation.build_request(
                transaction_id=self.transaction_id,
                receipt_path=self.receipt_path,
                receipt_descriptor=descriptor,
                config=self.config,
            )
            self._assert_core_bound()
            self._core = core
            self._core_bytes = content
            self._request = request
            self.state = ReceiptState.CORE_OPEN
            return self
        except BaseException:
            self.close()
            raise

    def request_attestation(self) -> dict[str, object]:
        self._require_state(ReceiptState.CORE_OPEN)
        assert self._request is not None
        self._assert_core_bound()
        try:
            supplied = self.signer(
                self._request,
                self._receipt_descriptor,
                self.config,
            )
            response = dict(supplied)
            runtime_attestation.verify_response_signature(
                response,
                request=self._request,
                config=self.config,
            )
        except (TypeError, ValueError, runtime_attestation.AttestationError) as exc:
            raise ReceiptStateError("runtime receipt Ed25519 attestation failed") from exc
        self._assert_core_bound()
        identity = response.get("receipt_core_identity")
        if not isinstance(identity, list) or tuple(identity) != self._core_identity:
            raise ReceiptStateError("runtime receipt attestation does not bind the opened core")
        self._response = response
        self.state = ReceiptState.ATTESTED
        return response

    def finalize(self) -> dict[str, object]:
        self._require_state(ReceiptState.ATTESTED)
        assert self._core is not None
        assert self._response is not None
        self._assert_core_bound()
        receipt = {**self._core, "producer_attestation": self._response}
        payload = _canonical_json(receipt)
        if len(payload) > self.config.max_receipt_bytes:
            raise ReceiptStateError("final runtime receipt exceeds its bounded size")
        try:
            self._create_journal()
            os.ftruncate(self._receipt_descriptor, 0)
            os.lseek(self._receipt_descriptor, 0, os.SEEK_SET)
            _write_all(self._receipt_descriptor, payload)
            os.fsync(self._receipt_descriptor)
        except OSError as exc:
            raise ReceiptStateError("runtime receipt could not be finalized") from exc
        final_identity = _identity(os.fstat(self._receipt_descriptor))
        self._assert_final_bound(final_identity)
        observed = _read_all(
            self._receipt_descriptor,
            limit=self.config.max_receipt_bytes,
        )
        after_read = _identity(os.fstat(self._receipt_descriptor))
        self._assert_final_bound(after_read)
        if observed != payload:
            raise ReceiptStateError("final runtime receipt bytes changed during verification")
        try:
            runtime_attestation.verify_runtime_build_attestation(
                receipt,
                config=self.config,
            )
        except runtime_attestation.AttestationError as exc:
            raise ReceiptStateError("final runtime receipt attestation is invalid") from exc
        self._assert_final_bound(after_read)
        self._commit_journal(after_read)
        self.state = ReceiptState.FINALIZED
        return receipt

    def _create_journal(self) -> None:
        self._require_state(ReceiptState.ATTESTED)
        self._assert_core_bound()
        if self._journal_descriptor >= 0:
            return
        expected = self._core_identity
        core = self._core_bytes
        if expected is None or core is None:
            raise ReceiptStateError("runtime receipt core journal inputs are unavailable")
        name = _journal_name(self.transaction_id)
        try:
            descriptor = os.open(
                name,
                _receipt_journal_flags(),
                RECEIPT_JOURNAL_MODE,
                dir_fd=self._parent_descriptor,
            )
            os.fchmod(descriptor, RECEIPT_JOURNAL_MODE)
            content = _journal_payload(
                state="prepared",
                identity=expected,
                core=core,
            )
            _write_journal_all(descriptor, content)
            os.fsync(descriptor)
            observed = _identity(os.fstat(descriptor))
            named = _identity(
                os.stat(name, dir_fd=self._parent_descriptor, follow_symlinks=False)
            )
            if (
                observed != named
                or observed[2] != RECEIPT_JOURNAL_MODE
                or observed[3] != 1
                or int(os.fstat(descriptor).st_uid) != self.config.preparer.uid
                or int(os.fstat(descriptor).st_gid) != self.config.preparer.gid
            ):
                raise ReceiptStateError("receipt journal publication is unsafe")
            os.fsync(self._parent_descriptor)
        except FileExistsError as exc:
            raise ReceiptStateError("receipt finalization journal already exists") from exc
        except BaseException as exc:
            if "descriptor" in locals():
                try:
                    _remove_bound_journal(
                        self._parent_descriptor,
                        name,
                        descriptor,
                    )
                except BaseException:
                    # A failed cleanup leaves the exact journal name for the
                    # next descriptor-bound recovery pass.  Never unlink a
                    # replacement merely to hide the original setup error.
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(exc, ReceiptStateError):
                raise
            raise ReceiptStateError("receipt finalization journal could not be created") from exc
        self._journal_name = name
        self._journal_descriptor = descriptor
        self._journal_identity = observed

    def _assert_journal_bound(self) -> None:
        if self._journal_descriptor < 0 or self._journal_name is None or self._journal_identity is None:
            raise ReceiptStateError("receipt finalization journal is unavailable")
        self._assert_parent_bound()
        try:
            opened = _identity(os.fstat(self._journal_descriptor))
            named = _identity(
                os.stat(
                    self._journal_name,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            )
        except (OSError, ReceiptStateError) as exc:
            raise ReceiptStateError("receipt finalization journal changed") from exc
        if opened != self._journal_identity or named != self._journal_identity:
            raise ReceiptStateError("receipt finalization journal changed")

    def _commit_journal(self, final_identity: ReceiptIdentity) -> None:
        self._assert_journal_bound()
        content = _journal_payload(
            state="committed",
            identity=self._core_identity or final_identity,
            core=None,
            final_identity=final_identity,
        )
        try:
            os.ftruncate(self._journal_descriptor, 0)
            os.lseek(self._journal_descriptor, 0, os.SEEK_SET)
            _write_journal_all(self._journal_descriptor, content)
            os.fsync(self._journal_descriptor)
            updated = _identity(os.fstat(self._journal_descriptor))
            named = _identity(
                os.stat(
                    self._journal_name,
                    dir_fd=self._parent_descriptor,
                    follow_symlinks=False,
                )
            )
            if (
                updated[:4] != self._journal_identity[:4]
                or named != updated
                or updated[2] != RECEIPT_JOURNAL_MODE
                or updated[3] != 1
            ):
                raise ReceiptStateError("receipt finalization journal changed while committing")
            self._journal_identity = updated
            self._assert_journal_bound()
            assert self._journal_name is not None
            _remove_bound_journal(
                self._parent_descriptor,
                self._journal_name,
                self._journal_descriptor,
            )
        except OSError as exc:
            raise ReceiptStateError("receipt finalization journal could not be committed") from exc
        self._journal_descriptor = -1
        self._journal_name = None
        self._journal_identity = None

    def run(self) -> dict[str, object]:
        self.open_core()
        self.request_attestation()
        return self.finalize()

    def close(self) -> None:
        if self.state is ReceiptState.CLOSED:
            return
        for descriptor in (
            self._journal_descriptor,
            self._receipt_descriptor,
            self._parent_descriptor,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self._receipt_descriptor = -1
        self._parent_descriptor = -1
        self._journal_descriptor = -1
        self._journal_name = None
        self._journal_identity = None
        self._parent_identity = None
        self.state = ReceiptState.CLOSED


def finalize_runtime_receipt(
    receipt_path: str | os.PathLike[str],
    transaction_id: str,
    *,
    config: runtime_attestation.DaemonConfig | None = None,
    signer: Signer = _default_signer,
) -> dict[str, object]:
    """Attest and finalize an already-published unsigned runtime core.

    This is the stable producer entry point.  No receipt bytes are written
    until the signer returns a response whose Ed25519 signature and complete
    core binding have been verified.  A signing or response-verification
    failure therefore leaves the exact canonical unsigned bytes on the same
    inode.  Successful finalization rewrites that retained inode in place.
    """

    resolved_config = runtime_attestation.production_config() if config is None else config
    _recover_receipt_journals(Path(receipt_path), resolved_config)
    finalizer = ReceiptFinalizer(
        receipt_path=Path(receipt_path),
        transaction_id=transaction_id,
        config=resolved_config,
        signer=signer,
    )
    try:
        return finalizer.run()
    finally:
        finalizer.close()


def recover_runtime_receipt(
    receipt_path: str | os.PathLike[str],
    *,
    config: runtime_attestation.DaemonConfig | None = None,
) -> None:
    """Consume any crash journal while retaining descriptor/path identity."""

    resolved_config = runtime_attestation.production_config() if config is None else config
    _recover_receipt_journals(Path(receipt_path), resolved_config)


def verify_runtime_receipt(
    receipt_path: str | os.PathLike[str],
    *,
    config: runtime_attestation.DaemonConfig | None = None,
) -> dict[str, object]:
    """Verify one final receipt from its exact public inode and return it.

    This is the stable read-only producer entry point for historical receipt
    provenance.  It authenticates the canonical receipt bytes with the public
    Ed25519 key and deliberately does not inspect current runtime source or
    output paths, which may already belong to a later preparation attempt.
    """

    resolved_config = runtime_attestation.production_config() if config is None else config
    path = _verification_path(receipt_path, resolved_config)
    if path == _absolute_canonical(resolved_config.runtime_receipt_path):
        _recover_receipt_journals(path, resolved_config)
    parent = _open_parent(path.parent)
    descriptor = -1
    try:
        parent_identity = _assert_preparer_parent(
            parent,
            resolved_config,
            label="runtime receipt verification parent",
        )
        descriptor = os.open(path.name, _receipt_read_flags(), dir_fd=parent)
        info = os.fstat(descriptor)
        expected = _identity(info)
        if expected[2] != RECEIPT_MODE or expected[3] != 1:
            raise ReceiptStateError("runtime receipt must have mode 0644 and one link")
        if (
            int(info.st_uid) != resolved_config.preparer.uid
            or int(info.st_gid) != resolved_config.preparer.gid
        ):
            raise ReceiptStateError("runtime receipt is not owned by the preparer")
        _assert_opened_receipt(
            parent_descriptor=parent,
            parent_identity=parent_identity,
            receipt_descriptor=descriptor,
            receipt_path=path,
            expected=expected,
        )
        content = _read_all(descriptor, limit=resolved_config.max_receipt_bytes)
        _assert_opened_receipt(
            parent_descriptor=parent,
            parent_identity=parent_identity,
            receipt_descriptor=descriptor,
            receipt_path=path,
            expected=expected,
        )
        receipt = _strict_json_object(content)
        if "producer_commitment" in receipt:
            raise ReceiptStateError("HMAC producer_commitment receipts are forbidden")
        if "producer_attestation" not in receipt:
            raise ReceiptStateError("runtime receipt lacks producer_attestation")
        if content != _canonical_json(receipt):
            raise ReceiptStateError("final runtime receipt is not canonical JSON")
        try:
            runtime_attestation.verify_runtime_build_attestation(
                receipt,
                config=resolved_config,
            )
        except runtime_attestation.AttestationError as exc:
            raise ReceiptStateError("final runtime receipt Ed25519 attestation is invalid") from exc
        _assert_opened_receipt(
            parent_descriptor=parent,
            parent_identity=parent_identity,
            receipt_descriptor=descriptor,
            receipt_path=path,
            expected=expected,
        )
        if _read_all(descriptor, limit=resolved_config.max_receipt_bytes) != content:
            raise ReceiptStateError("final runtime receipt bytes changed during verification")
        _assert_opened_receipt(
            parent_descriptor=parent,
            parent_identity=parent_identity,
            receipt_descriptor=descriptor,
            receipt_path=path,
            expected=expected,
        )
        return receipt
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Finalize a published TermDeck runtime receipt with its Ed25519 attestation"
    )
    parser.add_argument("--receipt", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--transaction-id")
    mode.add_argument("--verify-existing", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify_existing:
            receipt = verify_runtime_receipt(arguments.receipt)
        else:
            receipt = finalize_runtime_receipt(
                arguments.receipt,
                arguments.transaction_id,
            )
    except (ReceiptStateError, runtime_attestation.AttestationError, OSError) as exc:
        raise SystemExit(f"TermDeck receipt finalization refused: {exc}") from exc
    sys.stdout.write(_canonical_json(receipt).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReceiptFinalizer",
    "ReceiptState",
    "ReceiptStateError",
    "finalize_runtime_receipt",
    "main",
    "verify_runtime_receipt",
]
