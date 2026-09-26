"""Managed service wrappers for the TermDeck deployment.

The systemd units deliberately point at a small, release-bound set of files in
``t1000-managed``.  This module owns the description of that set and the
runtime checks performed by each wrapper.  Rendering is side-effect free;
publication is the only operation that writes files.

The wrapper files are ordinary Python executables.  A managed release binds
their import root and exact runtime modules to its verified ``site`` tree, so
a mutable checkout cannot change an already selected service.  The legacy
direct API defaults to its supplied source root when no release binding is
available.
"""

from __future__ import annotations

import grp
import hashlib
import json
import errno
import fcntl
import os
import pwd
import re
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeAlias


PathLike: TypeAlias = str | os.PathLike[str]


CANONICAL_SOURCE_ROOT = Path("/warp/projects/softwares/termdeck")
# TermDeck's deployment state lives beside the source checkout.  Keeping the
# runtime in this private dot-directory makes the source/runtime/state layout
# one physical tree and removes the retired runtime/state roots.
CANONICAL_RUNTIME_ROOT = CANONICAL_SOURCE_ROOT / ".termdeck"
CANONICAL_STATE_ROOT = CANONICAL_RUNTIME_ROOT / "state"
CANONICAL_MANAGED_ROOT = CANONICAL_RUNTIME_ROOT / "t1000-managed"
CANONICAL_MANAGED_BIN = CANONICAL_RUNTIME_ROOT / "bin"
CANONICAL_STABLE_LAUNCHER = CANONICAL_MANAGED_BIN / "t1000"
# Runtime receipts are authenticated by a privilege-separated root service.
# The unprivileged LAN runtime must never hold the signing key or a reusable
# signing capability; generated wrappers bind and export endpoint metadata
# only.
CANONICAL_CONTROL_ROOT = Path("/etc/termdeck/control")
CONTROL_ROOT_ENV = "TERMDECK_CONTROL_ROOT"
CONTROL_ATTEST_SOCKET_NAME = "runtime-attestd.sock"
# These values describe an endpoint; they are deliberately not a key, a
# reusable descriptor, or evidence that the caller is allowed to obtain an
# attestation.  The root service binds the peer-credential and one-shot
# transaction authorization independently.
CONTROL_ATTEST_OPERATION = "sign"
CONTROL_ATTEST_REQUEST_SCHEMA = "termdeck.lan-runtime-build.v2"
CONTROL_ATTEST_RESPONSE_SCHEMA = "termdeck.runtime-attestation.v1"
CONTROL_ATTEST_PRODUCER = "termdeck-preparer"
CONTROL_ATTEST_OWNER = "root"
CONTROL_ATTEST_GROUP = CONTROL_ATTEST_PRODUCER
CONTROL_ATTEST_EXCLUDED_PRINCIPAL = "max"
CONTROL_ROOT_MODE = 0o710
CONTROL_ATTEST_SOCKET_MODE = 0o660
PREPARER_BROKER_SOCKET = Path("/run/termdeck-preparer-broker/request.sock")
PREPARER_RUNTIME_ROOT = CANONICAL_RUNTIME_ROOT
PREPARER_BROKER_REQUEST_SCHEMA = "termdeck.preparer-broker-request.v1"
PREPARER_BROKER_RESPONSE_SCHEMA = "termdeck.preparer-broker-response.v1"
PREPARER_PUBLICATION_SCHEMA = "termdeck.preparer-publication.v1"
PREPARER_BROKER_MAX_FRAME_BYTES = 128 * 1024
PREPARER_BROKER_TIMEOUT = 190.0
DEFAULT_UNIT_DIR = Path("/home/arch/.config/systemd/user")

# The shorter aliases are kept public because deployment code and operational
# tooling tend to use these names when constructing a layout.
TERMDECK_SOURCE_ROOT = CANONICAL_SOURCE_ROOT
TERMDECK_RUNTIME_ROOT = CANONICAL_RUNTIME_ROOT
TERMDECK_STATE_ROOT = CANONICAL_STATE_ROOT
TERMDECK_MANAGED_ROOT = CANONICAL_MANAGED_ROOT
TERMDECK_BIN_DIR = CANONICAL_MANAGED_BIN
TERMDECK_STABLE_LAUNCHER = CANONICAL_STABLE_LAUNCHER

WRAPPER_MANIFEST_SCHEMA = "t1000.service-wrapper-manifest.v1"
UNIT_CONFIG_SCHEMA = "t1000.service-wrapper-config.v1"
WRAPPER_MANIFEST_NAME = "wrapper-manifest.json"
WRAPPER_MODE = 0o755
UNIT_CONFIG_MODE = 0o600
MAX_STOP_TIMEOUT = 60.0
DEFAULT_STOP_TIMEOUT = 30.0
CHECKPOINT_TIMEOUT = 120.0
CHECKPOINT_TERMINATE_GRACE = 5.0
CHECKPOINT_KILL_GRACE = 5.0
_SIGNAL_EINTR_RETRIES = 8


class WrapperError(RuntimeError):
    """A wrapper failed a path, release, command, or process identity check."""


@dataclass(frozen=True)
class UnitSpec:
    """Immutable policy for one managed service."""

    name: str
    stem: str
    stop_signal: int
    kind: str

    @property
    def start_name(self) -> str:
        return self.stem

    @property
    def stop_name(self) -> str:
        return f"{self.stem}-stop"


UNIT_SPECS: tuple[UnitSpec, ...] = (
    UnitSpec("termdeck-web-backend.service", "termdeck-web-backend", signal.SIGTERM, "backend"),
    UnitSpec("termdeck-web-lan.service", "termdeck-web-lan", signal.SIGQUIT, "lan"),
    UnitSpec("termdeck-web-recovery.service", "termdeck-web-recovery", signal.SIGTERM, "recovery"),
    UnitSpec("termdeck-cloudflared.service", "termdeck-cloudflared", signal.SIGTERM, "cloudflared"),
    UnitSpec("termdeck-cloudflare-preview.service", "termdeck-cloudflare-preview", signal.SIGTERM, "preview"),
    UnitSpec("termdeck-checkpoint.service", "termdeck-checkpoint", signal.SIGTERM, "checkpoint"),
)
UNIT_NAMES: tuple[str, ...] = tuple(spec.name for spec in UNIT_SPECS)
REQUIRED_UNITS = UNIT_NAMES
UNIT_SPEC_BY_NAME: dict[str, UnitSpec] = {spec.name: spec for spec in UNIT_SPECS}
WRAPPER_NAMES: tuple[str, ...] = tuple(
    name
    for spec in UNIT_SPECS
    for name in (spec.start_name, spec.stop_name)
)
WRAPPER_RELATIVE_NAMES = WRAPPER_NAMES
UNIT_CONFIG_NAMES: tuple[str, ...] = tuple(f"{name}.conf" for name in UNIT_NAMES)
_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "unit",
        "release_id",
        "pid",
        "start_ticks",
        "exe",
        "argv",
        "command_sha256",
        "state",
    }
)


def _canonical_json(value: object) -> bytes:
    """Return the one JSON representation used for generated metadata."""

    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path(value: PathLike, *, label: str) -> Path:
    """Parse an absolute lexical path and reject non-canonical spellings."""

    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise WrapperError(f"{label} is not a path: {value!r}") from exc
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if not raw or not os.path.isabs(raw):
        raise WrapperError(f"{label} must be absolute: {raw!r}")
    candidate = Path(raw)
    if any(part in (".", "..") for part in candidate.parts):
        raise WrapperError(f"{label} is not canonical: {raw!r}")
    # Path() normally normalises repeated separators.  Comparing the original
    # spelling to normpath catches // and trailing separators as well.
    if raw != os.path.normpath(raw):
        raise WrapperError(f"{label} is not canonical: {raw!r}")
    return candidate


def _assert_canonical(path: Path, *, label: str, must_exist: bool = False) -> None:
    """Ensure a path and every existing ancestor resolve to themselves."""

    checked = _path(path, label=label)
    exists = os.path.lexists(checked)
    if not exists:
        if must_exist:
            raise WrapperError(f"{label} does not exist: {checked}")
        # resolve(strict=False) still follows a symlink in an existing parent.
        try:
            resolved = checked.resolve(strict=False)
        except OSError as exc:
            raise WrapperError(f"could not resolve {label}: {checked}: {exc}") from exc
        if resolved != checked:
            raise WrapperError(f"{label} is below a symlinked path: {checked}")
        return
    try:
        mode = checked.lstat().st_mode
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {checked}: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise WrapperError(f"{label} may not be a symlink: {checked}")
    try:
        resolved = checked.resolve(strict=True)
    except OSError as exc:
        raise WrapperError(f"could not resolve {label}: {checked}: {exc}") from exc
    if resolved != checked:
        raise WrapperError(f"{label} resolves to a different path: {checked}")


def _assert_directory(path: Path, *, label: str, must_exist: bool = True) -> None:
    _assert_canonical(path, label=label, must_exist=must_exist)
    if not os.path.lexists(path):
        return
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise WrapperError(f"{label} is not a real directory: {path}")


_REQUIRED_EXECUTION_MODULES = frozenset(
    {
        "termdeck/__init__.py",
        "termdeck/service_wrappers.py",
        "termdeck/storage_fabric.py",
        "termdeck/web_activation.py",
    }
)


def _assert_execution_binding(layout: "WrapperLayout") -> None:
    """Verify the release-root modules a managed wrapper will import.

    Bindings are only emitted by release-manager after its full installed-site
    verification.  Rechecking the exact regular, single-linked, read-only
    files here prevents checkout drift and makes a post-publication mutation
    fail before either wrapper policy or recovery code is imported.
    """

    _assert_directory(layout.execution_root, label="wrapper execution root", must_exist=True)
    if not layout.execution_binding:
        return
    try:
        root_info = layout.execution_root.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect wrapper execution root: {exc}") from exc
    if stat.S_IMODE(root_info.st_mode) & 0o222:
        raise WrapperError("wrapper execution root is writable")
    bound_names = {name for name, _digest, _size in layout.execution_binding}
    if not _REQUIRED_EXECUTION_MODULES.issubset(bound_names):
        raise WrapperError("wrapper execution binding omits a required module")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not isinstance(nofollow, int) or isinstance(nofollow, bool) or nofollow == 0:
        raise WrapperError("wrapper execution binding requires O_NOFOLLOW")
    for name, expected_digest, expected_size in layout.execution_binding:
        candidate = layout.execution_root
        for component in PurePosixPath(name).parts[:-1]:
            candidate = candidate / component
            _assert_directory(
                candidate,
                label=f"wrapper execution directory {component}",
                must_exist=True,
            )
            try:
                directory_info = candidate.lstat()
            except OSError as exc:
                raise WrapperError(
                    f"could not inspect wrapper execution directory {component}: {exc}"
                ) from exc
            if stat.S_IMODE(directory_info.st_mode) & 0o222:
                raise WrapperError(f"wrapper execution directory is writable: {candidate}")
        candidate = candidate / PurePosixPath(name).parts[-1]
        _assert_canonical(candidate, label=f"wrapper execution module {name}", must_exist=True)
        try:
            before = candidate.lstat()
        except OSError as exc:
            raise WrapperError(f"could not inspect wrapper execution module {name}: {exc}") from exc
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            raise WrapperError(f"wrapper execution module is unsafe: {candidate}")
        try:
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
            )
        except OSError as exc:
            raise WrapperError(f"could not open wrapper execution module {name}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_nlink != 1
            ):
                raise WrapperError(f"wrapper execution module changed while opening: {candidate}")
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 131072)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = candidate.lstat()
        except OSError as exc:
            raise WrapperError(f"could not read wrapper execution module {name}: {exc}") from exc
        finally:
            os.close(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_nlink != 1
            or size != expected_size
            or digest.hexdigest() != expected_digest
        ):
            raise WrapperError(f"wrapper execution module changed: {candidate}")


def _is_within(path: Path, parent: Path) -> bool:
    """Return whether two lexical absolute paths share a subtree."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_control_binding(layout: "WrapperLayout") -> None:
    """Validate the non-secret root-control endpoint carried by a wrapper.

    ``max`` cannot traverse the control root.  The dedicated preparer receives
    group execute-only access to the socket namespace, while the root service
    still checks its exact peer credentials and independently validates the
    runtime descriptors before signing.  A same-UID key or socket would not
    provide this boundary.
    """

    root = layout.control_root
    if _is_within(root, layout.runtime_root) or _is_within(layout.runtime_root, root):
        raise WrapperError("wrapper control root must be isolated from the runtime root")
    if layout.execution_binding and layout.runtime_root == CANONICAL_RUNTIME_ROOT:
        if root != CANONICAL_CONTROL_ROOT:
            raise WrapperError("immutable production wrappers require the canonical control root")
        _assert_canonical(root, label="TermDeck control root", must_exist=True)
        try:
            info = root.lstat()
        except OSError as exc:
            raise WrapperError(f"could not inspect TermDeck control root: {exc}") from exc
        expected_group = _control_attestation_group_id()
        _assert_control_group_excludes_runtime_principal(expected_group)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != CONTROL_ROOT_MODE
            or info.st_uid != 0
            or info.st_gid != expected_group
        ):
            raise WrapperError(
                "TermDeck control root must be root:termdeck-preparer mode 0710"
            )


def _control_attestation_group_id() -> int:
    """Resolve the sole group allowed to traverse the root control namespace."""

    try:
        group = grp.getgrnam(CONTROL_ATTEST_GROUP)
    except (KeyError, OSError) as exc:
        raise WrapperError(
            "TermDeck control group termdeck-preparer is unavailable"
        ) from exc
    group_id = getattr(group, "gr_gid", None)
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0:
        raise WrapperError("TermDeck control group termdeck-preparer is invalid")
    return group_id


def _assert_control_group_excludes_runtime_principal(expected_group: int) -> None:
    """Ensure the max-owned runtime cannot directly call the root signer."""

    try:
        runtime_account = pwd.getpwnam(CONTROL_ATTEST_EXCLUDED_PRINCIPAL)
        memberships = os.getgrouplist(
            CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
            runtime_account.pw_gid,
        )
    except (KeyError, OSError, AttributeError) as exc:
        raise WrapperError(
            "TermDeck runtime principal max group membership is unavailable"
        ) from exc
    if any(group_id == expected_group for group_id in memberships):
        raise WrapperError(
            "TermDeck runtime principal max may not join termdeck-preparer"
        )


def _control_attestation_binding(layout: "WrapperLayout") -> dict[str, object]:
    """Return the public, non-secret protocol binding for one wrapper set.

    This record gives the immutable wrapper, runtime preparer, and activation
    code one exact endpoint and wire schema.  It never substitutes for the
    root attestor's descriptor validation, peer authorization, or one-shot
    transaction authorization.
    """

    return {
        "socket": str(layout.control_root / CONTROL_ATTEST_SOCKET_NAME),
        "operation": CONTROL_ATTEST_OPERATION,
        "request_schema": CONTROL_ATTEST_REQUEST_SCHEMA,
        "response_schema": CONTROL_ATTEST_RESPONSE_SCHEMA,
        "producer_principal": CONTROL_ATTEST_PRODUCER,
        "excluded_principal": CONTROL_ATTEST_EXCLUDED_PRINCIPAL,
        "control_root_owner": CONTROL_ATTEST_OWNER,
        "control_root_group": CONTROL_ATTEST_GROUP,
        "control_root_mode": CONTROL_ROOT_MODE,
        "socket_owner": CONTROL_ATTEST_OWNER,
        "socket_group": CONTROL_ATTEST_GROUP,
        "socket_mode": CONTROL_ATTEST_SOCKET_MODE,
    }


def control_attestation_binding(**kwargs: Any) -> dict[str, object]:
    """Return a validated, non-secret root-attestor endpoint contract.

    This performs only the control-namespace validation needed by release and
    activation callers; it intentionally does not open the socket or grant a
    signing capability to the caller.
    """

    layout = resolve_layout(**kwargs)
    _assert_control_binding(layout)
    return _control_attestation_binding(layout)


def _assert_destination(path: Path, *, label: str, executable: bool = False, mode: int | None = None) -> None:
    """Validate an existing publication destination without following links."""

    _assert_canonical(path, label=label, must_exist=False)
    if not os.path.lexists(path):
        return
    try:
        info = path.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WrapperError(f"{label} is not a regular file: {path}")
    if info.st_nlink != 1:
        raise WrapperError(f"{label} has unexpected hard links: {path}")
    permissions = stat.S_IMODE(info.st_mode)
    if executable:
        if permissions & 0o111 == 0:
            raise WrapperError(f"{label} is not executable: {path}")
        if permissions & 0o022:
            raise WrapperError(f"{label} is group/world writable: {path}")
    if mode is not None and permissions != mode:
        raise WrapperError(
            f"{label} has mode {permissions:04o}; expected {mode:04o}: {path}"
        )


def _layout_value(value: PathLike | None, default: Path, *, label: str) -> Path:
    return _path(default if value is None else value, label=label)


def _normalize_execution_binding(
    execution_root: Path,
    value: Mapping[str, object] | None,
) -> tuple[tuple[str, str, int], ...]:
    """Validate the immutable module rows embedded in a managed wrapper.

    The values are intentionally strict JSON-shaped rows.  They become part
    of both the generated wrapper source and manifest, so accepting a loose
    mapping here would make a different import root look equivalent at
    readback time.
    """

    del execution_root  # lexical validation happens before runtime file checks
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise WrapperError("wrapper execution binding must be a mapping")
    entries: list[tuple[str, str, int]] = []
    for raw_name, raw_row in value.items():
        if not isinstance(raw_name, str):
            raise WrapperError("wrapper execution binding path is invalid")
        name = PurePosixPath(raw_name)
        if (
            not raw_name
            or name.is_absolute()
            or str(name) != raw_name
            or any(part in {"", ".", ".."} for part in name.parts)
        ):
            raise WrapperError("wrapper execution binding path is invalid")
        if not isinstance(raw_row, Mapping) or set(raw_row) != {"sha256", "size"}:
            raise WrapperError("wrapper execution binding row is invalid")
        digest = raw_row["sha256"]
        size = raw_row["size"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise WrapperError("wrapper execution binding row is invalid")
        entries.append((raw_name, digest, size))
    # Generated legacy wrappers pass an empty JSON object explicitly.  It is
    # equivalent to the omitted binding and keeps their source deterministic.
    if not entries:
        return ()
    entries.sort()
    if len({name for name, _digest, _size in entries}) != len(entries):
        raise WrapperError("wrapper execution binding repeats a path")
    return tuple(entries)


def _execution_binding_payload(layout: "WrapperLayout") -> dict[str, dict[str, object]]:
    return {
        name: {"sha256": digest, "size": size}
        for name, digest, size in layout.execution_binding
    }


@dataclass(frozen=True)
class WrapperLayout:
    source_root: Path
    execution_root: Path
    execution_binding: tuple[tuple[str, str, int], ...]
    control_root: Path
    runtime_root: Path
    state_root: Path
    managed_root: Path
    managed_bin: Path
    unit_dir: Path
    manifest_path: Path

    def wrapper_path(self, spec: UnitSpec, action: str) -> Path:
        if action not in {"start", "stop"}:
            raise WrapperError(f"unknown wrapper action: {action!r}")
        name = spec.start_name if action == "start" else spec.stop_name
        return self.managed_root / name

    def config_path(self, spec: UnitSpec) -> Path:
        return self.state_root / f"{spec.name}.conf"


def resolve_layout(
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    root: PathLike | None = None,
) -> WrapperLayout:
    """Resolve and lexically validate all paths used by the wrapper set.

    ``root`` is a compatibility alias for ``managed_root``.  The function does
    not touch the filesystem; callers choose whether to run a read-only
    preflight or a publication operation afterwards.
    """

    if root is not None:
        if managed_root is not None:
            raise WrapperError("root and managed_root were both supplied")
        managed_root = root
    source = _layout_value(source_root, CANONICAL_SOURCE_ROOT, label="source root")
    execution = _layout_value(
        execution_root,
        source,
        label="wrapper execution root",
    )
    normalized_binding = _normalize_execution_binding(execution, execution_binding)
    runtime = _layout_value(runtime_root, CANONICAL_RUNTIME_ROOT, label="runtime root")
    if normalized_binding and control_root is None and runtime != CANONICAL_RUNTIME_ROOT:
        raise WrapperError("immutable wrapper execution requires an explicit control root")
    control = _layout_value(
        control_root,
        CANONICAL_CONTROL_ROOT,
        label="TermDeck control root",
    )
    state = _layout_value(state_root, CANONICAL_STATE_ROOT, label="state root")
    managed = _layout_value(
        managed_root,
        runtime / "t1000-managed" if runtime_root is not None else CANONICAL_MANAGED_ROOT,
        label="managed root",
    )
    binary = _layout_value(
        managed_bin,
        runtime / "bin" if runtime_root is not None else CANONICAL_MANAGED_BIN,
        label="managed bin",
    )
    units = _layout_value(unit_dir, DEFAULT_UNIT_DIR, label="unit directory")
    manifest = _layout_value(
        manifest_path,
        managed / WRAPPER_MANIFEST_NAME,
        label="wrapper manifest",
    )
    return WrapperLayout(
        source,
        execution,
        normalized_binding,
        control,
        runtime,
        state,
        managed,
        binary,
        units,
        manifest,
    )


def _spec_for(unit: str | UnitSpec) -> UnitSpec:
    if isinstance(unit, UnitSpec):
        return unit
    if unit in UNIT_SPEC_BY_NAME:
        return UNIT_SPEC_BY_NAME[unit]
    if unit.endswith(".service"):
        raise WrapperError(f"unsupported service unit: {unit!r}")
    candidate = f"{unit}.service"
    if candidate in UNIT_SPEC_BY_NAME:
        return UNIT_SPEC_BY_NAME[candidate]
    raise WrapperError(f"unsupported service unit: {unit!r}")


def _release_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WrapperError("release id must be a non-empty string")
    if any(char.isspace() or char in "/\\\x00" for char in value):
        raise WrapperError("release id contains an unsafe character")
    return value


def _runtime_paths(layout: WrapperLayout) -> dict[str, Path]:
    # Production uses the canonical .termdeck tree directly.  Keep the
    # historical nested shape for disposable caller-supplied layouts so old
    # test/rollback fixtures remain isolated from the production contract.
    lan = (
        layout.runtime_root
        if layout.runtime_root == CANONICAL_RUNTIME_ROOT
        else layout.runtime_root / "termdeck-lan"
    )
    return {
        "lan_root": lan,
        "backend": lan / "bin" / "termdeck-web-server",
        "prepare": lan / "bin" / "termdeck-web-prepare",
        "preview": lan / "bin" / "termdeck-cloudflare-preview",
        "nginx_root": lan / "nginx",
        "nginx_conf": lan / "nginx" / "nginx.conf",
        "nginx_pid": lan / "nginx" / "nginx.pid",
        "cloudflared_config": layout.runtime_root / "cloudflared" / "termdeck.yml",
        "database": layout.state_root / "sessions.sqlite3",
        "auth": lan / "auth",
        "recovery_evidence": layout.state_root / "recovery" / "activation-v2",
        "stable_launcher": layout.managed_bin / "t1000",
    }


def build_start_command(
    unit: str | UnitSpec,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    root: PathLike | None = None,
) -> tuple[str, ...]:
    """Build the exact post-wrapper command for a service start."""

    spec = _spec_for(unit)
    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        root=root,
    )
    paths = _runtime_paths(layout)
    if spec.kind == "backend":
        return (
            str(paths["backend"]),
            "--db",
            str(paths["database"]),
            "--listen",
            "127.0.0.1:7391",
            "--auth-dir",
            str(paths["auth"]),
            "--allow-insecure-loopback-auth",
            "--host-id",
            "termdeck-max-lan",
            "--owner-id",
            "shayan",
            "--refresh-ms",
            "750",
            "--max-stale-ms",
            "30000",
            "--max-connections",
            "256",
            "--tmux",
            "/usr/bin/tmux",
            "--t1000",
            str(paths["stable_launcher"]),
            "--tmux-timeout-ms",
            "1500",
            "--tab-action-timeout-ms",
            "120000",
        )
    if spec.kind == "lan":
        return (
            "/usr/bin/nginx",
            "-p",
            str(paths["nginx_root"]) + "/",
            "-c",
            str(paths["nginx_conf"]),
            "-g",
            "daemon off;",
        )
    if spec.kind == "recovery":
        return (
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            (
                "import runpy,sys;"
                "sys.path.insert(0,sys.argv.pop(1));"
                "runpy.run_module('termdeck.web_activation',run_name='__main__')"
            ),
            str(layout.execution_root),
            "--watchdog",
            "--latest",
            "--target",
            str(layout.source_root),
            "--evidence",
            str(paths["recovery_evidence"]),
            "--runtime",
            str(paths["lan_root"]),
            "--database",
            str(paths["database"]),
            "--managed-root",
            str(layout.managed_root),
            "--managed-bin",
            str(layout.managed_bin),
            "--unit-dir",
            str(layout.unit_dir),
        )
    if spec.kind == "cloudflared":
        return (
            "/usr/bin/cloudflared",
            "--config",
            str(paths["cloudflared_config"]),
            "--no-autoupdate",
            "tunnel",
            "run",
        )
    if spec.kind == "preview":
        return (str(paths["preview"]),)
    if spec.kind == "checkpoint":
        return ()
    raise WrapperError(f"no command policy for {spec.name}")


def start_command(unit: str | UnitSpec, **kwargs: Any) -> tuple[str, ...]:
    return build_start_command(unit, **kwargs)


def build_stop_command(
    unit: str | UnitSpec,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    root: PathLike | None = None,
) -> tuple[str, ...]:
    """Build the only stop-side subprocess command (the checkpoint)."""

    spec = _spec_for(unit)
    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        root=root,
    )
    if spec.kind != "checkpoint":
        return ()
    return (
        "/usr/bin/runuser",
        "-u",
        "max",
        "--",
        "/usr/bin/env",
        f"HOME={layout.state_root}",
        f"PATH={layout.managed_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
        str(layout.managed_bin / "t1000"),
        "checkpoint",
        "--all",
    )


def stop_command(unit: str | UnitSpec, **kwargs: Any) -> tuple[str, ...]:
    return build_stop_command(unit, **kwargs)


def _generated_execution_guard(layout: WrapperLayout) -> str:
    """Return the immutable-release check embedded in generated wrappers."""

    if not layout.execution_binding:
        return ""
    root = json.dumps(str(layout.execution_root))
    binding = json.dumps(_execution_binding_payload(layout), sort_keys=True)
    return (
        f"_execution_root = {root}\n"
        f"_execution_binding = {binding}\n"
        "if os.path.realpath(_execution_root) != _execution_root:\n"
        "    raise RuntimeError('wrapper execution root is symlinked')\n"
        "_root_info = os.lstat(_execution_root)\n"
        "if (not stat.S_ISDIR(_root_info.st_mode) or _root_info.st_mode & 0o222):\n"
        "    raise RuntimeError('wrapper execution root is mutable')\n"
        "for _relative, _expected in _execution_binding.items():\n"
        "    _candidate = os.path.join(_execution_root, *_relative.split('/'))\n"
        "    _before = os.lstat(_candidate)\n"
        "    if (stat.S_ISLNK(_before.st_mode) or not stat.S_ISREG(_before.st_mode)\n"
        "            or _before.st_nlink != 1 or _before.st_mode & 0o222):\n"
        "        raise RuntimeError('wrapper execution module is unsafe: ' + _relative)\n"
        "    _flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)\n"
        "    _fd = os.open(_candidate, _flags)\n"
        "    try:\n"
        "        _after = os.fstat(_fd)\n"
        "        if (_after.st_dev != _before.st_dev or _after.st_ino != _before.st_ino\n"
        "                or _after.st_nlink != 1 or not stat.S_ISREG(_after.st_mode)):\n"
        "            raise RuntimeError('wrapper execution module changed: ' + _relative)\n"
        "        _hash = hashlib.sha256()\n"
        "        while True:\n"
        "            _chunk = os.read(_fd, 131072)\n"
        "            if not _chunk:\n"
        "                break\n"
        "            _hash.update(_chunk)\n"
        "    finally:\n"
        "        os.close(_fd)\n"
        "    if _after.st_size != _expected['size'] or _hash.hexdigest() != _expected['sha256']:\n"
        "        raise RuntimeError('wrapper execution module digest changed: ' + _relative)\n"
    )


def _script_source(spec: UnitSpec, layout: WrapperLayout, release_id: str, action: str) -> bytes:
    # json.dumps produces a deterministic quoted Python string for every safe
    # path/release value accepted above.  The source module remains the single
    # implementation of all checks, so generated wrappers contain no policy
    # that can drift from readback and stop verification.
    literals = [
        json.dumps(spec.name),
        json.dumps(action),
        json.dumps(release_id),
        json.dumps(str(layout.source_root)),
        json.dumps(str(layout.execution_root)),
        json.dumps(_execution_binding_payload(layout), sort_keys=True),
        json.dumps(str(layout.control_root)),
        json.dumps(str(layout.runtime_root)),
        json.dumps(str(layout.state_root)),
        json.dumps(str(layout.managed_root)),
        json.dumps(str(layout.managed_bin)),
        json.dumps(str(layout.unit_dir)),
        json.dumps(str(layout.manifest_path)),
    ]
    source = (
        "#!/usr/bin/python3\n"
        "# t1000 managed wrapper; generated by termdeck.service_wrappers\n"
        "import hashlib\n"
        "import os\n"
        "import stat\n"
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        f"os.environ[{json.dumps(CONTROL_ROOT_ENV)}] = {literals[6]}\n"
        + _generated_execution_guard(layout)
        + f"sys.path.insert(0, {literals[4]})\n"
        "from termdeck.service_wrappers import main as _main\n"
        f"raise SystemExit(_main(unit={literals[0]}, action={literals[1]}, release_id={literals[2]}, "
        f"source_root={literals[3]}, execution_root={literals[4]}, execution_binding={literals[5]}, "
        f"control_root={literals[6]}, runtime_root={literals[7]}, state_root={literals[8]}, "
        f"managed_root={literals[9]}, managed_bin={literals[10]}, unit_dir={literals[11]}, "
        f"manifest_path={literals[12]}))\n"
    )
    return source.encode("utf-8")


def _initial_config(spec: UnitSpec, layout: WrapperLayout, release_id: str) -> dict[str, object]:
    return {
        "schema": UNIT_CONFIG_SCHEMA,
        "unit": spec.name,
        "release_id": release_id,
        "pid": None,
        "start_ticks": None,
        "exe": None,
        "argv": [],
        "command_sha256": None,
        "state": "stopped",
    }


def _manifest_core(
    *,
    layout: WrapperLayout,
    release_id: str,
    wrappers: Mapping[Path, bytes],
    configs: Mapping[Path, bytes],
) -> dict[str, object]:
    wrapper_entries: list[dict[str, object]] = []
    config_entries: list[dict[str, object]] = []
    for spec in UNIT_SPECS:
        start = layout.wrapper_path(spec, "start")
        stop = layout.wrapper_path(spec, "stop")
        config = layout.config_path(spec)
        wrapper_entries.extend(
            (
                {
                    "unit": spec.name,
                    "action": "start",
                    "name": start.name,
                    "path": str(start),
                    "sha256": _sha256_bytes(wrappers[start]),
                    "mode": WRAPPER_MODE,
                },
                {
                    "unit": spec.name,
                    "action": "stop",
                    "name": stop.name,
                    "path": str(stop),
                    "sha256": _sha256_bytes(wrappers[stop]),
                    "mode": WRAPPER_MODE,
                },
            )
        )
        config_entries.append(
            {
                "unit": spec.name,
                "name": config.name,
                "path": str(config),
                "sha256": _sha256_bytes(configs[config]),
                "mode": UNIT_CONFIG_MODE,
            }
        )
    return {
        "schema": WRAPPER_MANIFEST_SCHEMA,
        "release_id": release_id,
        "source_root": str(layout.source_root),
        "execution_root": str(layout.execution_root),
        "execution_binding": _execution_binding_payload(layout),
        "control_root": str(layout.control_root),
        "control_socket": str(layout.control_root / CONTROL_ATTEST_SOCKET_NAME),
        "control_attestation": _control_attestation_binding(layout),
        "runtime_root": str(layout.runtime_root),
        "state_root": str(layout.state_root),
        "managed_root": str(layout.managed_root),
        "managed_bin": str(layout.managed_bin),
        "unit_dir": str(layout.unit_dir),
        "wrappers": wrapper_entries,
        "configs": config_entries,
    }


def _manifest_bytes(core: Mapping[str, object]) -> bytes:
    digest = _sha256_bytes(_canonical_json(core))
    payload = dict(core)
    payload["manifest_sha256"] = digest
    return _canonical_json(payload)


class RenderedWrappers(Mapping[Path, bytes]):
    """Pure render result.

    The mapping interface intentionally exposes only the twelve executable
    wrappers.  Config and manifest bytes are available as named attributes and
    through :meth:`all_files` for publication.
    """

    def __init__(
        self,
        *,
        layout: WrapperLayout,
        release_id: str,
        wrappers: Mapping[Path, bytes],
        configs: Mapping[Path, bytes],
        manifest: bytes,
    ) -> None:
        self.layout = layout
        self.release_id = release_id
        self.wrappers = dict(wrappers)
        self.configs = dict(configs)
        self.manifest = manifest

    @property
    def manifest_path(self) -> Path:
        return self.layout.manifest_path

    def __getitem__(self, key: Path | str) -> bytes:
        if isinstance(key, str):
            for path, content in self.wrappers.items():
                if path.name == key:
                    return content
        return self.wrappers[key]  # type: ignore[index]

    def __iter__(self) -> Iterator[Path]:
        return iter(self.wrappers)

    def __len__(self) -> int:
        return len(self.wrappers)

    def all_files(self) -> dict[Path, bytes]:
        result = dict(self.wrappers)
        result.update(self.configs)
        result[self.manifest_path] = self.manifest
        return result

    @property
    def files(self) -> dict[Path, bytes]:
        return self.all_files()

    @property
    def wrapper_files(self) -> dict[Path, bytes]:
        return dict(self.wrappers)

    @property
    def config_files(self) -> dict[Path, bytes]:
        return dict(self.configs)

    @property
    def manifest_bytes(self) -> bytes:
        return self.manifest

    @property
    def wrapper_names(self) -> tuple[str, ...]:
        return tuple(path.name for path in self.wrappers)


@dataclass(frozen=True)
class WrapperPublicationPlan:
    """A side-effect-free wrapper write set bound to its anchored preimage.

    ``release_manager`` uses this plan when wrapper publication must share a
    journal with a broader activation transaction.  Keeping the config carry
    logic here means that a combined transaction has exactly the same live
    PID/argv preservation semantics as :func:`publish_wrappers`.
    """

    layout: WrapperLayout
    rendered: RenderedWrappers
    snapshot: Mapping[Path, FileSnapshot]
    parent_guards: object
    writes: tuple[tuple[Path, bytes, int, str], ...]

    def verify(self) -> None:
        readback_wrappers(
            source_root=self.layout.source_root,
            execution_root=self.layout.execution_root,
            execution_binding=_execution_binding_payload(self.layout),
            control_root=self.layout.control_root,
            runtime_root=self.layout.runtime_root,
            state_root=self.layout.state_root,
            managed_root=self.layout.managed_root,
            managed_bin=self.layout.managed_bin,
            unit_dir=self.layout.unit_dir,
            manifest_path=self.layout.manifest_path,
            expected_release_id=self.rendered.release_id,
        )


def render_wrappers(
    release_id: str,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    root: PathLike | None = None,
) -> RenderedWrappers:
    """Render exactly twelve deterministic wrapper files and metadata."""

    release = _release_id(release_id)
    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        manifest_path=manifest_path,
        root=root,
    )
    wrappers: dict[Path, bytes] = {}
    configs: dict[Path, bytes] = {}
    for spec in UNIT_SPECS:
        wrappers[layout.wrapper_path(spec, "start")] = _script_source(spec, layout, release, "start")
        wrappers[layout.wrapper_path(spec, "stop")] = _script_source(spec, layout, release, "stop")
        configs[layout.config_path(spec)] = _canonical_json(_initial_config(spec, layout, release))
    core = _manifest_core(layout=layout, release_id=release, wrappers=wrappers, configs=configs)
    return RenderedWrappers(
        layout=layout,
        release_id=release,
        wrappers=wrappers,
        configs=configs,
        manifest=_manifest_bytes(core),
    )


def render_wrapper_files(*args: Any, **kwargs: Any) -> dict[Path, bytes]:
    """Return only the twelve wrapper bytes for callers that need a plain map."""

    return dict(render_wrappers(*args, **kwargs).wrappers)


def render_unit_configs(*args: Any, **kwargs: Any) -> dict[Path, bytes]:
    return dict(render_wrappers(*args, **kwargs).configs)


def _publication_paths(layout: WrapperLayout) -> tuple[Path, ...]:
    """Return the complete, fixed publication set in one canonical order."""

    return (
        *(
            layout.wrapper_path(spec, action)
            for spec in UNIT_SPECS
            for action in ("start", "stop")
        ),
        *(layout.config_path(spec) for spec in UNIT_SPECS),
        layout.manifest_path,
    )


def _release_publication_api() -> object:
    """Load descriptor-anchored publication primitives without an import cycle."""

    try:
        from . import release_manager
    except Exception as exc:  # pragma: no cover - package import is expected
        raise WrapperError(f"transaction primitive unavailable: {exc}") from exc
    return release_manager


def _wrapper_parent_guards(
    layout: WrapperLayout,
    *,
    create_parents: bool,
) -> object:
    api = _release_publication_api()
    try:
        return api._capture_publication_parent_guards(  # type: ignore[attr-defined]
            _publication_paths(layout),
            create_parents=create_parents,
        )
    except Exception as exc:
        if isinstance(exc, WrapperError):
            raise
        raise WrapperError(f"could not anchor wrapper publication parents: {exc}") from exc


def _assert_wrapper_parent_guards(
    layout: WrapperLayout,
    parent_guards: object,
    *,
    create_parents: bool,
) -> None:
    api = _release_publication_api()
    try:
        parents = api._open_publication_parents(  # type: ignore[attr-defined]
            list(_publication_paths(layout)),
            parent_guards=parent_guards,
            create_parents=create_parents,
        )
        try:
            parents.assert_current(phase="during wrapper preflight")
        finally:
            parents.close()
    except Exception as exc:
        if isinstance(exc, WrapperError):
            raise
        raise WrapperError(f"wrapper publication parent identity changed: {exc}") from exc


def _ensure_output_directories(
    layout: WrapperLayout,
    *,
    parent_guards: object | None = None,
) -> object:
    """Safely create and anchor only the wrapper output parents.

    The release manager primitive walks every ancestor using ``openat`` and
    retains its device/inode chain.  A supplied guard comes from the release
    preimage phase, so a parent swap between phases fails here rather than
    letting a later publication discover a replacement directory.
    """

    guards = parent_guards or _wrapper_parent_guards(layout, create_parents=True)
    _assert_wrapper_parent_guards(layout, guards, create_parents=True)
    # Other roots are read-only inputs.  Do not create a typo in one of them.
    for directory, label in (
        (layout.source_root, "source root"),
        (layout.runtime_root, "runtime root"),
        (layout.managed_bin, "managed bin"),
    ):
        _assert_directory(directory, label=label, must_exist=True)
    _assert_execution_binding(layout)
    _assert_control_binding(layout)
    _assert_directory(layout.manifest_path.parent, label="wrapper manifest parent", must_exist=True)
    return guards


def _preflight_destinations(
    layout: WrapperLayout,
    *,
    parent_guards: object | None = None,
) -> object:
    guards = _ensure_output_directories(layout, parent_guards=parent_guards)
    for spec in UNIT_SPECS:
        _assert_destination(
            layout.wrapper_path(spec, "start"),
            label=f"{spec.name} start wrapper",
            executable=True,
            mode=WRAPPER_MODE,
        )
        _assert_destination(
            layout.wrapper_path(spec, "stop"),
            label=f"{spec.name} stop wrapper",
            executable=True,
            mode=WRAPPER_MODE,
        )
        _assert_destination(layout.config_path(spec), label=f"{spec.name} config", mode=UNIT_CONFIG_MODE)
    _assert_destination(layout.manifest_path, label="wrapper manifest", mode=UNIT_CONFIG_MODE)
    expected = set(WRAPPER_NAMES)
    try:
        entries = list(layout.managed_root.iterdir())
    except OSError as exc:
        raise WrapperError(f"could not inspect managed root: {layout.managed_root}: {exc}") from exc
    for entry in entries:
        if entry.name.startswith("termdeck-") or entry.name.endswith("-stop"):
            if entry.name not in expected:
                raise WrapperError(f"unexpected managed wrapper entry: {entry}")
    config_expected = set(UNIT_CONFIG_NAMES)
    try:
        state_entries = list(layout.state_root.iterdir())
    except OSError as exc:
        raise WrapperError(f"could not inspect state root: {layout.state_root}: {exc}") from exc
    for entry in state_entries:
        if entry.name.startswith("termdeck-") and entry.name.endswith(".service.conf"):
            if entry.name not in config_expected:
                raise WrapperError(f"unexpected managed service config: {entry}")
    return guards


def _load_json_file(path: Path, *, label: str, max_bytes: int = 4 * 1024 * 1024) -> dict[str, object]:
    _assert_destination(path, label=label)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise WrapperError(f"could not read {label}: {path}: {exc}") from exc
    if len(content) > max_bytes:
        raise WrapperError(f"{label} is too large: {path}")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WrapperError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise WrapperError(f"{label} must contain an object: {path}")
    return value


def _validate_manifest(
    layout: WrapperLayout,
    *,
    expected_release_id: str | None = None,
) -> dict[str, object]:
    _assert_destination(layout.manifest_path, label="wrapper manifest", mode=UNIT_CONFIG_MODE)
    manifest = _load_json_file(layout.manifest_path, label="wrapper manifest")
    if manifest.get("schema") != WRAPPER_MANIFEST_SCHEMA:
        raise WrapperError("wrapper manifest schema mismatch")
    release = manifest.get("release_id")
    if not isinstance(release, str):
        raise WrapperError("wrapper manifest release id is invalid")
    _release_id(release)
    if expected_release_id is not None and release != _release_id(expected_release_id):
        raise WrapperError("wrapper manifest release id does not match requested release")
    stored_digest = manifest.get("manifest_sha256")
    if not isinstance(stored_digest, str):
        raise WrapperError("wrapper manifest has no digest")
    core = dict(manifest)
    del core["manifest_sha256"]
    if _sha256_bytes(_canonical_json(core)) != stored_digest:
        raise WrapperError("wrapper manifest digest mismatch")
    for key, value in (
        ("source_root", str(layout.source_root)),
        ("execution_root", str(layout.execution_root)),
        ("control_root", str(layout.control_root)),
        ("control_socket", str(layout.control_root / CONTROL_ATTEST_SOCKET_NAME)),
        ("runtime_root", str(layout.runtime_root)),
        ("state_root", str(layout.state_root)),
        ("managed_root", str(layout.managed_root)),
        ("managed_bin", str(layout.managed_bin)),
        ("unit_dir", str(layout.unit_dir)),
    ):
        if manifest.get(key) != value:
            raise WrapperError(f"wrapper manifest {key} mismatch")
    if manifest.get("control_attestation") != _control_attestation_binding(layout):
        raise WrapperError("wrapper manifest control attestation mismatch")
    if manifest.get("execution_binding") != _execution_binding_payload(layout):
        raise WrapperError("wrapper manifest execution binding mismatch")
    wrappers = manifest.get("wrappers")
    configs = manifest.get("configs")
    if not isinstance(wrappers, list) or len(wrappers) != 12:
        raise WrapperError("wrapper manifest does not describe exactly twelve wrappers")
    if not isinstance(configs, list) or len(configs) != 6:
        raise WrapperError("wrapper manifest does not describe six configs")
    return manifest


def _validate_manifest_entries(layout: WrapperLayout, manifest: Mapping[str, object]) -> None:
    expected_wrappers: dict[tuple[str, str], Path] = {}
    for spec in UNIT_SPECS:
        expected_wrappers[(spec.name, "start")] = layout.wrapper_path(spec, "start")
        expected_wrappers[(spec.name, "stop")] = layout.wrapper_path(spec, "stop")
    entries = manifest.get("wrappers")
    assert isinstance(entries, list)
    seen: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise WrapperError("wrapper manifest contains a malformed wrapper entry")
        key = (item.get("unit"), item.get("action"))
        if key not in expected_wrappers or key in seen:
            raise WrapperError("wrapper manifest contains an unexpected wrapper entry")
        seen.add(key)
        path = expected_wrappers[key]
        if item.get("name") != path.name or item.get("path") != str(path):
            raise WrapperError("wrapper manifest path mismatch")
        if item.get("mode") != WRAPPER_MODE:
            raise WrapperError("wrapper manifest mode mismatch")
        digest = item.get("sha256")
        if not isinstance(digest, str) or _sha256_bytes(path.read_bytes()) != digest:
            raise WrapperError(f"wrapper digest mismatch: {path}")
    if seen != set(expected_wrappers):
        raise WrapperError("wrapper manifest wrapper set is incomplete")

    expected_configs = {spec.name: layout.config_path(spec) for spec in UNIT_SPECS}
    config_entries = manifest.get("configs")
    assert isinstance(config_entries, list)
    seen_configs: set[str] = set()
    for item in config_entries:
        if not isinstance(item, dict):
            raise WrapperError("wrapper manifest contains a malformed config entry")
        unit = item.get("unit")
        if not isinstance(unit, str) or unit not in expected_configs or unit in seen_configs:
            raise WrapperError("wrapper manifest contains an unexpected config entry")
        seen_configs.add(unit)
        path = expected_configs[unit]
        if item.get("name") != path.name or item.get("path") != str(path):
            raise WrapperError("wrapper config path mismatch")
        if item.get("mode") != UNIT_CONFIG_MODE:
            raise WrapperError("wrapper config mode mismatch")
        # Config files are deliberately mutable: a running start wrapper
        # records its PID, start ticks, executable, and argv in this file.
        # The manifest binds the deterministic template digest for audit
        # purposes, while readback verifies the release/unit identity below
        # and leaves the runtime identity fields available to stop.
        config = _read_unit_config(
            path,
            spec=_spec_for(unit),
            expected_release_id=str(manifest.get("release_id")),
        )
        if (
            config.get("state") == "stopped"
            and config.get("pid") is None
            and config.get("start_ticks") is None
            and config.get("exe") is None
            and config.get("argv") == []
        ):
            digest = item.get("sha256")
            if not isinstance(digest, str) or _sha256_bytes(path.read_bytes()) != digest:
                raise WrapperError(f"wrapper config template digest mismatch: {path}")
    if seen_configs != set(expected_configs):
        raise WrapperError("wrapper manifest config set is incomplete")


def readback_wrappers(
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    expected_release_id: str | None = None,
    release_id: str | None = None,
    root: PathLike | None = None,
) -> dict[str, object]:
    """Read and verify the complete published wrapper set."""

    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        manifest_path=manifest_path,
        root=root,
    )
    if release_id is not None:
        expected_release_id = release_id
    for directory, label in (
        (layout.source_root, "source root"),
        (layout.execution_root, "wrapper execution root"),
        (layout.runtime_root, "runtime root"),
        (layout.state_root, "state root"),
        (layout.managed_root, "managed root"),
        (layout.managed_bin, "managed bin"),
        (layout.unit_dir, "unit directory"),
    ):
        _assert_directory(directory, label=label, must_exist=True)
    _assert_execution_binding(layout)
    _assert_control_binding(layout)
    for spec in UNIT_SPECS:
        _assert_destination(layout.wrapper_path(spec, "start"), label=f"{spec.name} start wrapper", executable=True, mode=WRAPPER_MODE)
        _assert_destination(layout.wrapper_path(spec, "stop"), label=f"{spec.name} stop wrapper", executable=True, mode=WRAPPER_MODE)
        _assert_destination(layout.config_path(spec), label=f"{spec.name} config", mode=UNIT_CONFIG_MODE)
    _assert_destination(layout.manifest_path, label="wrapper manifest")
    expected_wrappers = set(WRAPPER_NAMES)
    try:
        managed_entries = list(layout.managed_root.iterdir())
        state_entries = list(layout.state_root.iterdir())
    except OSError as exc:
        raise WrapperError(f"could not inspect wrapper publication roots: {exc}") from exc
    for entry in managed_entries:
        if (entry.name.startswith("termdeck-") or entry.name.endswith("-stop")) and entry.name not in expected_wrappers:
            raise WrapperError(f"unexpected managed wrapper entry: {entry}")
    expected_configs = set(UNIT_CONFIG_NAMES)
    for entry in state_entries:
        if entry.name.startswith("termdeck-") and entry.name.endswith(".service.conf") and entry.name not in expected_configs:
            raise WrapperError(f"unexpected managed service config: {entry}")
    manifest = _validate_manifest(layout, expected_release_id=expected_release_id)
    _validate_manifest_entries(layout, manifest)
    return {
        "ok": True,
        "schema": WRAPPER_MANIFEST_SCHEMA,
        "release_id": manifest["release_id"],
        "manifest_path": str(layout.manifest_path),
        "managed_root": str(layout.managed_root),
        "execution_root": str(layout.execution_root),
        "execution_binding": _execution_binding_payload(layout),
        "control_root": str(layout.control_root),
        "control_socket": str(layout.control_root / CONTROL_ATTEST_SOCKET_NAME),
        "control_attestation": _control_attestation_binding(layout),
        "wrapper_names": list(WRAPPER_NAMES),
        "config_paths": [str(layout.config_path(spec)) for spec in UNIT_SPECS],
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest": manifest,
    }


def preflight_wrappers(**kwargs: Any) -> dict[str, object]:
    """Run a read-only preflight and return the verified readback."""

    return readback_wrappers(**kwargs)


# The release manager's primitive is intentionally imported lazily.  This
# avoids a module cycle when release_manager imports wrapper publication during
# an install while still using its fsync and rollback implementation.
def _publish_files_transactionally(
    writes: Sequence[tuple[Path, bytes | None, int, str]],
    *,
    verify: Callable[[], None] | None = None,
    parent_guards: object | None = None,
    expected_preimages: Mapping[Path, object] | None = None,
) -> None:
    try:
        from .release_manager import _publish_files_transactionally as publish
    except Exception as exc:  # pragma: no cover - package import is expected
        raise WrapperError(f"transaction primitive unavailable: {exc}") from exc
    try:
        publish(
            tuple(writes),
            verify=verify,
            parent_guards=parent_guards,
            expected_preimages=expected_preimages,
        )
    except Exception as exc:
        if isinstance(exc, WrapperError):
            raise
        raise WrapperError(str(exc)) from exc


@dataclass(frozen=True)
class FileSnapshot:
    """A wrapper preimage plus the leaf identity read through its parent FD.

    ``None`` identity fields denote an explicitly absent leaf or a legacy
    content-only caller.  Snapshots captured by this module always carry the
    complete identity for an existing file; publication passes it unchanged to
    the descriptor transaction for leaf compare-and-swap fencing.
    """

    exists: bool
    content: bytes = b""
    mode: int = 0o600
    device: int | None = None
    inode: int | None = None
    nlink: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    sha256: str | None = None


_SNAPSHOT_IDENTITY_FIELDS = (
    "device",
    "inode",
    "nlink",
    "size",
    "mtime_ns",
    "ctime_ns",
    "sha256",
)
_SNAPSHOT_FIELDS = frozenset(("exists", "content", "mode", *_SNAPSHOT_IDENTITY_FIELDS))
_LEGACY_SNAPSHOT_FIELDS = frozenset(("exists", "content", "mode"))


def _validated_snapshot_identity(
    *,
    exists: object,
    content: object,
    mode: object,
    values: Mapping[str, object],
    path: Path,
    identity_present: bool,
) -> FileSnapshot:
    """Validate a snapshot without coercing mapping values or identities."""

    if not isinstance(exists, bool):
        raise WrapperError(f"malformed snapshot existence for {path}")
    if not isinstance(content, bytes):
        raise WrapperError(f"malformed snapshot content for {path}")
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
        raise WrapperError(f"malformed snapshot mode for {path}")
    if not exists and content:
        raise WrapperError(f"absent snapshot contains bytes for {path}")

    metadata = {name: values[name] for name in _SNAPSHOT_IDENTITY_FIELDS}
    if not identity_present:
        return FileSnapshot(exists, content, mode)
    if not exists:
        if any(value is not None for value in metadata.values()):
            raise WrapperError(f"absent snapshot has an identity for {path}")
        return FileSnapshot(exists, content, mode)
    for name in _SNAPSHOT_IDENTITY_FIELDS[:-1]:
        value = metadata[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise WrapperError(f"malformed snapshot {name} for {path}")
    digest = metadata["sha256"]
    if (
        int(metadata["device"]) < 0
        or int(metadata["inode"]) <= 0
        or int(metadata["nlink"]) != 1
        or int(metadata["size"]) < 0
        or int(metadata["size"]) != len(content)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or digest != _sha256_bytes(content)
    ):
        raise WrapperError(f"malformed snapshot identity for {path}")
    return FileSnapshot(
        exists,
        content,
        mode,
        int(metadata["device"]),
        int(metadata["inode"]),
        int(metadata["nlink"]),
        int(metadata["size"]),
        int(metadata["mtime_ns"]),
        int(metadata["ctime_ns"]),
        digest,
    )


def _snapshot_content_matches(left: FileSnapshot, right: FileSnapshot) -> bool:
    """Compare a desired readback without requiring its old inode to recur."""

    return (
        left.exists == right.exists
        and left.content == right.content
        and left.mode == right.mode
        and (
            not left.exists
            or right.sha256 is None
            or left.sha256 == right.sha256
        )
    )


class _WrapperSnapshot(dict[Path, FileSnapshot]):
    """A preimage map bound to the parent identities it was read through.

    It remains an ordinary mapping for callers that persist or compare the
    preimage, while direct restoration can reject a replacement directory
    that appeared after the capture.
    """

    __slots__ = ("parent_guards",)

    def __init__(
        self,
        values: Mapping[Path, FileSnapshot],
        *,
        parent_guards: object,
    ) -> None:
        super().__init__(values)
        self.parent_guards = parent_guards


def _snapshot_parent_guards(value: object) -> object | None:
    if isinstance(value, Mapping) and "snapshot" in value:
        value = value["snapshot"]
    return getattr(value, "parent_guards", None)


def _capture_snapshot(
    layout: WrapperLayout,
    *,
    parent_guards: object | None = None,
) -> dict[Path, FileSnapshot]:
    """Capture preimages with no-follow descriptor-relative reads."""

    guards = parent_guards or _ensure_output_directories(layout)
    api = _release_publication_api()
    paths = _publication_paths(layout)
    try:
        snapshots = api._capture_anchored_files_safely(  # type: ignore[attr-defined]
            tuple((path, f"publication destination {path}") for path in paths),
            parent_guards=guards,
        )
    except Exception as exc:
        raise WrapperError(f"could not capture wrapper publication preimages: {exc}") from exc
    result: dict[Path, FileSnapshot] = {}
    for path in paths:
        item = snapshots.get(path)
        if item is None:
            raise WrapperError(f"wrapper publication preimage is incomplete: {path}")
        base = getattr(item, "snapshot", None)
        identity = getattr(item, "identity", None)
        digest = getattr(item, "sha256", None)
        if base is None:
            raise WrapperError(f"wrapper publication preimage is malformed: {path}")
        exists = getattr(base, "exists", None)
        content = getattr(base, "content", None)
        mode = getattr(base, "mode", None)
        if not exists:
            result[path] = _validated_snapshot_identity(
                exists=exists,
                content=content,
                mode=mode,
                values={name: None for name in _SNAPSHOT_IDENTITY_FIELDS},
                path=path,
                identity_present=True,
            )
            continue
        values = {
            "device": getattr(identity, "device", None),
            "inode": getattr(identity, "inode", None),
            "nlink": getattr(identity, "nlink", None),
            "size": getattr(identity, "size", None),
            "mtime_ns": getattr(identity, "mtime_ns", None),
            "ctime_ns": getattr(identity, "ctime_ns", None),
            "sha256": digest,
        }
        result[path] = _validated_snapshot_identity(
            exists=exists,
            content=content,
            mode=mode,
            values=values,
            path=path,
            identity_present=True,
        )
    return _WrapperSnapshot(result, parent_guards=guards)


def capture_wrapper_snapshot(
    *,
    _parent_guards: object | None = None,
    **kwargs: Any,
) -> dict[Path, FileSnapshot]:
    layout = resolve_layout(**kwargs)
    return _capture_snapshot(layout, parent_guards=_parent_guards)


def _snapshot_from(value: object) -> Mapping[Path, FileSnapshot]:
    if isinstance(value, Mapping) and "snapshot" in value:
        value = value["snapshot"]
    if not isinstance(value, Mapping):
        raise WrapperError("wrapper restore requires a file snapshot")
    result: dict[Path, FileSnapshot] = {}
    for raw_path, raw_snapshot in value.items():
        path = _path(raw_path, label="snapshot path")
        if isinstance(raw_snapshot, FileSnapshot):
            exists = raw_snapshot.exists
            content = raw_snapshot.content
            mode = raw_snapshot.mode
            values = {
                name: getattr(raw_snapshot, name)
                for name in _SNAPSHOT_IDENTITY_FIELDS
            }
            identity_present = any(item is not None for item in values.values())
        else:
            if not isinstance(raw_snapshot, Mapping) or set(raw_snapshot) not in {
                _LEGACY_SNAPSHOT_FIELDS,
                _SNAPSHOT_FIELDS,
            }:
                raise WrapperError(f"malformed snapshot for {path}")
            exists = raw_snapshot["exists"]
            content = raw_snapshot["content"]
            mode = raw_snapshot["mode"]
            identity_present = set(raw_snapshot) == _SNAPSHOT_FIELDS
            values = {
                name: raw_snapshot.get(name)
                for name in _SNAPSHOT_IDENTITY_FIELDS
            }
        result[path] = _validated_snapshot_identity(
            exists=exists,
            content=content,
            mode=mode,
            values=values,
            path=path,
            identity_present=identity_present,
        )
    return result


def restore_wrappers(
    snapshot: object,
    *,
    _parent_guards: object | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    """Restore a captured wrapper publication transactionally."""

    layout = resolve_layout(**kwargs)
    bound_guards = _parent_guards or _snapshot_parent_guards(snapshot)
    if bound_guards is None:
        raise WrapperError("wrapper restore requires a parent-bound preimage")
    parent_guards = _ensure_output_directories(layout, parent_guards=bound_guards)
    captured = _snapshot_from(snapshot)
    expected_paths = set(_publication_paths(layout))
    if set(captured) != expected_paths:
        raise WrapperError("snapshot does not cover the complete wrapper publication")
    for path in expected_paths:
        _assert_destination(path, label=f"restore destination {path}")
    current = _capture_snapshot(layout, parent_guards=parent_guards)

    writes: list[tuple[Path, bytes | None, int, str]] = []
    for path in sorted(expected_paths, key=str):
        item = captured[path]
        writes.append(
            (
                path,
                item.content if item.exists else None,
                item.mode,
                f"restore {path.name}",
            )
        )

    def verify_restore() -> None:
        observed = _capture_snapshot(layout, parent_guards=parent_guards)
        if any(
            not _snapshot_content_matches(observed[path], captured[path])
            for path in expected_paths
        ):
            raise WrapperError("wrapper restore readback does not match its preimage")
        # A complete prior publication must also pass its semantic manifest,
        # wrapper digest, configuration, and mode checks.  Partial preimages
        # are valid recovery targets after a failed repair, so their secure
        # byte-for-byte readback above is the applicable complete check.
        if all(item.exists for item in captured.values()):
            readback_wrappers(
                source_root=layout.source_root,
                execution_root=layout.execution_root,
                execution_binding=_execution_binding_payload(layout),
                control_root=layout.control_root,
                runtime_root=layout.runtime_root,
                state_root=layout.state_root,
                managed_root=layout.managed_root,
                managed_bin=layout.managed_bin,
                unit_dir=layout.unit_dir,
                manifest_path=layout.manifest_path,
            )

    _publish_files_transactionally(
        writes,
        verify=verify_restore,
        parent_guards=parent_guards,
        expected_preimages=current,
    )
    return {"ok": True, "restored": True, "paths": [str(path) for path in sorted(expected_paths, key=str)]}


def prepare_wrapper_publication(
    release_id: str | RenderedWrappers,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    root: PathLike | None = None,
    _parent_guards: object | None = None,
    _snapshot: object | None = None,
) -> WrapperPublicationPlan:
    """Build one anchored wrapper write plan without changing destinations.

    The optional snapshot is intentionally internal.  It lets a release-wide
    transaction use the exact preimages authenticated during release
    preflight, instead of recapturing a weaker content-only view after another
    publication has begun.
    """

    if isinstance(release_id, RenderedWrappers):
        if any(
            value is not None
            for value in (
                source_root,
                execution_root,
                execution_binding,
                control_root,
                runtime_root,
                state_root,
                managed_root,
                managed_bin,
                unit_dir,
                manifest_path,
                root,
            )
        ):
            raise WrapperError("rendered wrappers cannot be combined with layout overrides")
        rendered = release_id
    else:
        rendered = render_wrappers(
            release_id,
            source_root=source_root,
            execution_root=execution_root,
            execution_binding=execution_binding,
            control_root=control_root,
            runtime_root=runtime_root,
            state_root=state_root,
            managed_root=managed_root,
            managed_bin=managed_bin,
            unit_dir=unit_dir,
            manifest_path=manifest_path,
            root=root,
        )
    layout = rendered.layout
    parent_guards = _preflight_destinations(layout, parent_guards=_parent_guards)
    if _snapshot is None:
        snapshot: Mapping[Path, FileSnapshot] = _capture_snapshot(
            layout,
            parent_guards=parent_guards,
        )
    else:
        captured = _snapshot_from(_snapshot)
        expected_paths = set(_publication_paths(layout))
        if set(captured) != expected_paths:
            raise WrapperError("snapshot does not cover the complete wrapper publication")
        snapshot = _WrapperSnapshot(captured, parent_guards=parent_guards)
    writes: list[tuple[Path, bytes, int, str]] = []
    for path, content in rendered.wrappers.items():
        writes.append((path, content, WRAPPER_MODE, f"wrapper {path.name}"))
    for spec in UNIT_SPECS:
        path = layout.config_path(spec)
        content = _carry_runtime_identity(
            spec,
            path,
            rendered.configs[path],
            existing_content=(snapshot[path].content if snapshot[path].exists else None),
        )
        writes.append((path, content, UNIT_CONFIG_MODE, f"config {path.name}"))
    writes.append((layout.manifest_path, rendered.manifest, UNIT_CONFIG_MODE, "wrapper manifest"))
    return WrapperPublicationPlan(
        layout=layout,
        rendered=rendered,
        snapshot=snapshot,
        parent_guards=parent_guards,
        writes=tuple(writes),
    )


def publish_wrappers(
    release_id: str | RenderedWrappers,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    root: PathLike | None = None,
    _parent_guards: object | None = None,
) -> dict[str, object]:
    """Atomically publish wrappers, six configs, and their release manifest."""

    plan = prepare_wrapper_publication(
        release_id,
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        manifest_path=manifest_path,
        root=root,
        _parent_guards=_parent_guards,
    )
    _publish_files_transactionally(
        plan.writes,
        verify=plan.verify,
        parent_guards=plan.parent_guards,
        expected_preimages=plan.snapshot,
    )
    return {
        "ok": True,
        "release_id": plan.rendered.release_id,
        "managed_root": str(plan.layout.managed_root),
        "state_root": str(plan.layout.state_root),
        "execution_root": str(plan.layout.execution_root),
        "execution_binding": _execution_binding_payload(plan.layout),
        "control_root": str(plan.layout.control_root),
        "control_socket": str(plan.layout.control_root / CONTROL_ATTEST_SOCKET_NAME),
        "control_attestation": _control_attestation_binding(plan.layout),
        "manifest_path": str(plan.layout.manifest_path),
        "manifest_sha256": json.loads(plan.rendered.manifest.decode("utf-8"))["manifest_sha256"],
        "wrapper_names": list(WRAPPER_NAMES),
        "snapshot": plan.snapshot,
        "rendered": plan.rendered,
    }


def publish(*args: Any, **kwargs: Any) -> dict[str, object]:
    return publish_wrappers(*args, **kwargs)


def _regular_executable(path: Path, *, label: str, allow_system_symlink: bool = False) -> Path:
    if allow_system_symlink:
        # Distribution managed system commands such as /usr/bin/python3 and
        # /usr/bin/nginx may be a final symlink.  Their resolved target still
        # has to be canonical and regular; the managed runtime files below
        # remain subject to the stricter no-symlink check.
        path = _path(path, label=label)
        if not os.path.lexists(path):
            raise WrapperError(f"{label} does not exist: {path}")
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                path = path.resolve(strict=True)
                _assert_canonical(path, label=label, must_exist=True)
            else:
                _assert_canonical(path, label=label, must_exist=True)
        except OSError as exc:
            raise WrapperError(f"could not resolve {label}: {path}: {exc}") from exc
    else:
        _assert_canonical(path, label=label, must_exist=True)
    try:
        info = path.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        if not allow_system_symlink:
            raise WrapperError(f"{label} may not be a symlink: {path}")
        path = path.resolve(strict=True)
        info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise WrapperError(f"{label} is not an executable regular file: {path}")
    return path


def _regular_input(path: Path, *, label: str, readable: bool = True) -> None:
    _assert_canonical(path, label=label, must_exist=True)
    try:
        info = path.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WrapperError(f"{label} is not a regular file: {path}")
    if readable and not os.access(path, os.R_OK):
        raise WrapperError(f"{label} is not readable: {path}")


def _private_config(path: Path, *, label: str) -> None:
    _regular_input(path, label=label)
    try:
        if stat.S_IMODE(path.lstat().st_mode) & 0o022:
            raise WrapperError(f"{label} is group/world writable: {path}")
    except OSError as exc:
        raise WrapperError(f"could not inspect {label}: {path}: {exc}") from exc


def _broker_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WrapperError("preparer broker message is not canonicalizable") from exc


def _broker_receive_exact(connection: socket.socket, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        try:
            block = connection.recv(size - len(content))
        except (OSError, socket.timeout) as exc:
            raise WrapperError("could not read preparer broker response") from exc
        if not block:
            raise WrapperError("preparer broker response ended early")
        content.extend(block)
    return bytes(content)


def _validate_preparer_publication_result(result: object, *, operation: str = "publish") -> None:
    """Require the fixed publisher's complete consumer observation."""

    if operation not in ("publish", "verify"):
        raise WrapperError("preparer broker publication operation is invalid")
    fields = {
        "schema", "operation", "ok", "generation", "producer_receipt_sha256",
        "receipt", "outputs", "tls",
    }
    if (
        not isinstance(result, dict)
        or set(result) not in (fields, fields | {"cleanup_warnings"})
        or result.get("schema") != PREPARER_PUBLICATION_SCHEMA
        or result.get("operation") != operation
        or result.get("ok") is not True
        or not isinstance(result.get("generation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", result["generation"]) is None
    ):
        raise WrapperError("preparer broker publication result is invalid")
    if "cleanup_warnings" in result:
        warnings = result["cleanup_warnings"]
        if (
            not isinstance(warnings, list)
            or len(warnings) > 8
            or any(
                not isinstance(warning, str) or not warning.strip() or len(warning) > 512
                for warning in warnings
            )
            or len(_broker_json(warnings)) > 4096
        ):
            raise WrapperError("preparer broker publication warnings are invalid")

    def validate_claim(value: object, *, path: str, mode: int) -> None:
        if not isinstance(value, dict) or set(value) != {"path", "mode", "identity", "sha256"}:
            raise WrapperError(f"preparer broker publication claim is invalid: {path}")
        identity = value["identity"]
        digest = value["sha256"]
        if (
            value["path"] != path
            or type(value["mode"]) is not int
            or value["mode"] != mode
            or not isinstance(identity, list)
            or len(identity) != 7
            or any(type(item) is not int or item < 0 for item in identity)
            or identity[1] == 0
            or identity[2] != mode
            or identity[3] != 1
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise WrapperError(f"preparer broker publication claim is invalid: {path}")

    validate_claim(result["receipt"], path="build-receipt.json", mode=0o644)
    if result["producer_receipt_sha256"] != result["receipt"]["sha256"]:
        raise WrapperError("preparer broker publication receipt digest differs from producer")
    outputs = result["outputs"]
    if not isinstance(outputs, dict) or set(outputs) != {"current-lan-ip", "nginx/allowed-hosts.map"}:
        raise WrapperError("preparer broker publication outputs are invalid")
    for path, claim in outputs.items():
        validate_claim(claim, path=path, mode=0o600)

    tls = result["tls"]
    if not isinstance(tls, dict) or set(tls) != {"path", "identity", "manifest"}:
        raise WrapperError("preparer broker publication TLS result is invalid")
    identity = tls["identity"]
    if (
        tls["path"] != "tls"
        or not isinstance(identity, list)
        or len(identity) != 4
        or any(type(item) is not int or item < 0 for item in identity)
        or identity[1] == 0
        or identity[2] != 0o700
        or identity[3] != os.geteuid()
    ):
        raise WrapperError("preparer broker publication TLS identity is invalid")
    tls_modes = {
        "ca.crt": 0o644,
        "ca.key": 0o600,
        "server.key": 0o600,
        "server.crt": 0o644,
        "server-chain.crt": 0o644,
        "server.ext": 0o600,
    }
    manifest = tls["manifest"]
    if not isinstance(manifest, dict) or set(manifest) not in (set(tls_modes), set(tls_modes) | {"ca.srl"}):
        raise WrapperError("preparer broker publication TLS manifest is invalid")
    for path, claim in manifest.items():
        validate_claim(claim, path=path, mode=tls_modes.get(path, 0o644))


def _request_lan_preparation(
    *,
    socket_path: Path = PREPARER_BROKER_SOCKET,
    timeout: float = PREPARER_BROKER_TIMEOUT,
    restore_mode: str = "none",
    generation: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object] | None:
    """Ask the fixed root broker to run one preparation as termdeck-preparer."""

    if runtime_root is not None:
        _assert_canonical(runtime_root, label="LAN runtime root", must_exist=True)
        if runtime_root != PREPARER_RUNTIME_ROOT:
            raise WrapperError(
                "the fixed preparer broker cannot serve a non-canonical runtime root"
            )
    _assert_canonical(socket_path, label="preparer broker socket", must_exist=True)
    try:
        info = socket_path.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect preparer broker socket: {socket_path}: {exc}") from exc
    try:
        caller = pwd.getpwuid(os.geteuid())
        caller_gid = int(caller.pw_gid)
    except (KeyError, OSError, TypeError, ValueError, AttributeError) as exc:
        raise WrapperError("could not resolve the broker caller primary group") from exc
    if caller_gid < 0:
        raise WrapperError("broker caller primary group is invalid")
    if (
        not stat.S_ISSOCK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or int(info.st_uid) != os.geteuid()
        or int(info.st_gid) != caller_gid
    ):
        raise WrapperError("preparer broker socket ownership, mode, or primary group is unsafe")
    if restore_mode not in {"none", "generation", "unreceipted", "verify"}:
        raise WrapperError("preparer broker restore mode is invalid")
    if restore_mode == "generation":
        if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
            raise WrapperError("preparer broker restore generation is invalid")
    elif generation is not None:
        raise WrapperError("preparer broker restore generation is invalid")
    request = {
        "operation": "prepare",
        "restore": {"generation": generation, "mode": restore_mode},
        "schema": PREPARER_BROKER_REQUEST_SCHEMA,
    }
    payload = _broker_json(request)
    if not payload or len(payload) > PREPARER_BROKER_MAX_FRAME_BYTES:
        raise WrapperError("preparer broker request exceeds its bounded frame")
    # The one-request service can send its response before systemd observes
    # its exit. FlushPending then discards a connection queued during that
    # handoff. Only a read-only verification may be replayed; preparation and
    # restoration can already have taken effect when their connection fails.
    attempts = 3 if restore_mode == "verify" else 1
    deadline = time.monotonic() + timeout
    for attempt in range(attempts):
        if attempt:
            _assert_canonical(socket_path, label="preparer broker socket", must_exist=True)
            try:
                current = socket_path.lstat()
            except OSError as exc:
                raise WrapperError("could not reinspect preparer broker socket") from exc
            if (
                current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid
            ) != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid):
                raise WrapperError("preparer broker socket changed during verification handoff")
        response_started = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("preparer broker request deadline expired")
                connection.settimeout(remaining)
                connection.connect(os.fspath(socket_path))
                connection.sendall(struct.pack("!I", len(payload)) + payload)
                header = connection.recv(4)
                if not header:
                    raise EOFError("preparer broker response ended before its frame")
                response_started = True
                header += _broker_receive_exact(connection, 4 - len(header))
                length = struct.unpack("!I", header)[0]
                if not 0 < length <= PREPARER_BROKER_MAX_FRAME_BYTES:
                    raise WrapperError("preparer broker response frame is invalid")
                response_payload = _broker_receive_exact(connection, length)
                if connection.recv(1):
                    raise WrapperError("preparer broker sent trailing response bytes")
            break
        except WrapperError:
            raise
        except (OSError, EOFError) as exc:
            handoff_disconnect = isinstance(exc, EOFError) or (
                isinstance(exc, OSError) and exc.errno == errno.ECONNRESET
            )
            delay = 0.1 * (attempt + 1)
            if (
                handoff_disconnect
                and not response_started
                and attempt + 1 < attempts
                and deadline - time.monotonic() > delay
            ):
                time.sleep(delay)
                continue
            raise WrapperError(f"preparer broker request failed: {exc}") from exc

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                raise WrapperError("preparer broker response has duplicate fields")
            parsed[key] = value
        return parsed

    try:
        response = json.loads(
            response_payload.decode("ascii"),
            object_pairs_hook=no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WrapperError("preparer broker response is not strict JSON") from exc
    if response_payload != _broker_json(response):
        raise WrapperError("preparer broker response is not canonical JSON")
    if (
        not isinstance(response, dict)
        or not {"error", "ok", "returncode", "schema"}.issubset(response)
        or response.get("schema") != PREPARER_BROKER_RESPONSE_SCHEMA
        or response.get("ok") is not True
        or type(response.get("returncode")) is not int
        or response.get("returncode") != 0
        or response.get("error") is not None
    ):
        raise WrapperError("preparer broker refused runtime preparation")
    result = response.get("result")
    if restore_mode == "none":
        if set(response) != {"error", "ok", "result", "returncode", "schema"}:
            raise WrapperError("preparer broker returned an unexpected preparation result")
        _validate_preparer_publication_result(result)
        return None
    if not isinstance(result, dict):
        raise WrapperError("preparer broker returned no attested restore result")
    return result


def _admit_backend_tmux_runtime(layout: WrapperLayout) -> None:
    """Export the same storage admission used by the managed desktop launcher.

    The Rust backend survives this wrapper's exec. Only durable paths and an
    identity tuple cross that boundary; an open directory descriptor does not.
    """
    from .storage_fabric import StorageFabricError, require_storage_fabric, tmux_subprocess_environment

    runtime = layout.state_root / "tmux"
    try:
        require_storage_fabric(
            layout.managed_root,
            db_path=layout.state_root / "sessions.sqlite3",
            auth_root=layout.state_root / "terminal-auth",
            tmux_runtime=runtime,
            runtime_root=layout.state_root / "runtime",
            checkpoint_root=layout.state_root / "checkpoints",
            production=True,
        )
        child = tmux_subprocess_environment(materialize=True)
    except StorageFabricError as exc:
        raise WrapperError(f"backend tmux admission failed: {exc}") from exc
    if child is None:
        raise WrapperError("backend tmux admission produced no runtime binding")
    metadata = runtime.lstat()
    os.environ["TERMDECK_STATE_ROOT"] = str(layout.state_root)
    os.environ["TERMDECK_TMUX_RUNTIME"] = str(runtime)
    os.environ["TMUX_TMPDIR"] = str(runtime)
    os.environ["TERMDECK_TMUX_IDENTITY"] = f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_uid}"
    os.environ.pop("TMUX", None)


def _preflight_unit_start(spec: UnitSpec, layout: WrapperLayout) -> tuple[str, ...]:
    paths = _runtime_paths(layout)
    if spec.kind == "checkpoint":
        # Checkpoint start intentionally has no side effects and does not read
        # or rewrite its config.
        return ()
    if spec.kind == "backend":
        _regular_executable(paths["backend"], label="web backend")
        _regular_executable(paths["stable_launcher"], label="stable t1000 launcher")
        _regular_executable(Path("/usr/bin/tmux"), label="tmux", allow_system_symlink=True)
        _regular_input(paths["database"], label="session database")
        _assert_directory(paths["auth"], label="backend auth directory", must_exist=True)
        _admit_backend_tmux_runtime(layout)
    elif spec.kind == "lan":
        _regular_executable(Path("/usr/bin/nginx"), label="nginx", allow_system_symlink=True)
        _regular_input(paths["nginx_conf"], label="nginx configuration")
    elif spec.kind == "recovery":
        _regular_executable(Path("/usr/bin/python3"), label="recovery Python", allow_system_symlink=True)
        _assert_execution_binding(layout)
        if not layout.execution_binding:
            # Legacy releases did not carry a verified wheel-site copy of the
            # activation module.  Keep their established source input check
            # while current managed releases reject this unbound path.
            _regular_input(layout.source_root / "scripts" / "termdeck-web-activate.py", label="recovery watchdog")
        _assert_directory(paths["lan_root"], label="LAN runtime", must_exist=True)
        _regular_input(paths["database"], label="recovery session database")
    elif spec.kind == "cloudflared":
        _regular_executable(Path("/usr/bin/cloudflared"), label="cloudflared", allow_system_symlink=True)
        _private_config(paths["cloudflared_config"], label="cloudflared configuration")
    elif spec.kind == "preview":
        _regular_executable(paths["preview"], label="Cloudflare preview helper")
    command = build_start_command(
        spec,
        source_root=layout.source_root,
        execution_root=layout.execution_root,
        execution_binding=_execution_binding_payload(layout),
        control_root=layout.control_root,
        runtime_root=layout.runtime_root,
        state_root=layout.state_root,
        managed_root=layout.managed_root,
        managed_bin=layout.managed_bin,
        unit_dir=layout.unit_dir,
    )
    if spec.kind == "lan":
        # nginx -t is deliberately run before the identity is recorded.  A
        # failed config test cannot leave a stop file pointing at this wrapper.
        _request_lan_preparation(runtime_root=paths["lan_root"])
        subprocess.run(
            (
                "/usr/bin/nginx",
                "-t",
                "-p",
                str(paths["nginx_root"]) + "/",
                "-c",
                str(paths["nginx_conf"]),
            ),
            check=True,
        )
    return command


def proc_start_ticks(pid: int) -> int:
    """Read Linux ``/proc`` start ticks for a process."""

    if not isinstance(pid, int) or pid <= 0:
        raise WrapperError(f"invalid process id: {pid!r}")
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise WrapperError(f"could not read process start time for {pid}: {exc}") from exc
    closing = line.rfind(")")
    if closing < 0:
        raise WrapperError(f"malformed process stat for {pid}")
    fields = line[closing + 1 :].split()
    # The text after the final ')' starts at field 3 (state), so field 22 is
    # index 19 in this slice.
    if len(fields) <= 19:
        raise WrapperError(f"malformed process stat for {pid}")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise WrapperError(f"malformed process start time for {pid}") from exc


def proc_executable(pid: int) -> str:
    if not isinstance(pid, int) or pid <= 0:
        raise WrapperError(f"invalid process id: {pid!r}")
    try:
        value = os.readlink(f"/proc/{pid}/exe")
    except OSError as exc:
        raise WrapperError(f"could not read process executable for {pid}: {exc}") from exc
    if value.endswith(" (deleted)"):
        value = value[: -len(" (deleted)")]
    try:
        return str(Path(value).resolve(strict=True))
    except OSError as exc:
        raise WrapperError(f"process executable is unavailable for {pid}: {value}") from exc


def proc_argv(pid: int) -> tuple[str, ...]:
    if not isinstance(pid, int) or pid <= 0:
        raise WrapperError(f"invalid process id: {pid!r}")
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise WrapperError(f"could not read process argv for {pid}: {exc}") from exc
    if not raw:
        return ()
    values = raw.split(b"\0")
    if values and values[-1] == b"":
        values.pop()
    try:
        return tuple(value.decode("utf-8", errors="surrogateescape") for value in values)
    except Exception as exc:  # pragma: no cover - decode is surrogate-safe
        raise WrapperError(f"could not decode process argv for {pid}") from exc


def process_identity(pid: int) -> dict[str, object]:
    return {"pid": pid, "start_ticks": proc_start_ticks(pid), "exe": proc_executable(pid), "argv": list(proc_argv(pid))}


read_process_identity = process_identity


def _expected_proc_executable(command_executable: str) -> str:
    path = _path(command_executable, label="command executable")
    # A system command may use a final distribution symlink.  Resolve that
    # component for the /proc identity while still rejecting a symlinked
    # ancestor or an unresolved target.
    if not os.path.lexists(path):
        raise WrapperError(f"command executable does not exist: {path}")
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            path = path.resolve(strict=True)
            _assert_canonical(path, label="command executable", must_exist=True)
        else:
            _assert_canonical(path, label="command executable", must_exist=True)
    except OSError as exc:
        raise WrapperError(f"could not resolve command executable: {path}") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise WrapperError(f"could not inspect command executable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        path = path.resolve(strict=True)
        info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WrapperError(f"command executable is not regular: {path}")
    # A script's /proc/exe is its interpreter after exec.  Resolve the common
    # shebang forms so stop identity checks remain exact for the preview and
    # watchdog helpers.
    try:
        first = path.open("rb").readline(256)
    except OSError:
        first = b""
    if first.startswith(b"#!"):
        fields = shlex.split(first[2:].decode("utf-8", errors="replace").strip())
        if fields:
            interpreter = fields[0]
            if interpreter == "/usr/bin/env" and len(fields) > 1:
                found = next(
                    (
                        candidate
                        for candidate in os.get_exec_path()
                        if os.path.exists(os.path.join(candidate, fields[1]))
                    ),
                    None,
                )
                if found is not None:
                    interpreter = os.path.join(found, fields[1])
            if os.path.isabs(interpreter) and os.path.exists(interpreter):
                try:
                    return str(Path(interpreter).resolve(strict=True))
                except OSError:
                    pass
    try:
        return str(path.resolve(strict=True))
    except OSError as exc:
        raise WrapperError(f"could not resolve command executable: {path}") from exc


def _expected_proc_argv(command: Sequence[str]) -> tuple[str, ...]:
    """Return the argv Linux exposes after executing a script by its path."""

    if not command:
        raise WrapperError("command is empty")
    path = _path(command[0], label="command executable")
    if os.path.lexists(path) and stat.S_ISLNK(path.lstat().st_mode):
        path = path.resolve(strict=True)
    _assert_canonical(path, label="command executable", must_exist=True)
    try:
        first = path.open("rb").readline(256)
    except OSError:
        first = b""
    if not first.startswith(b"#!"):
        return tuple(str(item) for item in command)
    fields = shlex.split(first[2:].decode("utf-8", errors="replace").strip())
    if not fields:
        return tuple(str(item) for item in command)
    # Linux places the interpreter and its optional shebang argument before
    # the script path.  Keep the raw interpreter spelling because that is what
    # /proc/<pid>/cmdline exposes, while proc_executable resolves it separately.
    return tuple([fields[0], *fields[1:], str(path), *(str(item) for item in command[1:])])


def _process_argv_matches(
    current: tuple[str, ...],
    expected: tuple[str, ...],
    *,
    executable: str,
) -> bool:
    if current == expected:
        return True
    if not current or not expected or expected[0] != "/usr/bin/nginx":
        return False
    try:
        nginx_executable = str(Path("/usr/bin/nginx").resolve(strict=True))
    except OSError:
        return False
    # nginx replaces argv with this exact master title, joining its original
    # arguments with spaces. Linux may expose the remaining NUL padding as
    # empty arguments. Keep raw /proc snapshots unchanged and admit this one
    # representation only for the expected system nginx executable.
    return (
        executable == nginx_executable
        and current[0] == "nginx: master process " + " ".join(expected)
        and all(value == "" for value in current[1:])
    )


def verify_process_identity(
    identity: Mapping[str, object],
    *,
    pid: int | None = None,
    executable: str | None = None,
    argv: Sequence[str] | None = None,
) -> bool:
    """Verify PID reuse resistance using all recorded /proc identity fields."""

    try:
        recorded_pid = int(identity["pid"] if pid is None else pid)
        recorded_ticks = int(identity["start_ticks"])
        recorded_exe = str(identity["exe"] if executable is None else executable)
        recorded_argv = tuple(str(item) for item in (identity.get("argv", []) if argv is None else argv))
    except (KeyError, TypeError, ValueError) as exc:
        raise WrapperError("incomplete process identity") from exc
    if recorded_pid <= 0 or recorded_ticks <= 0 or not recorded_exe or not recorded_argv:
        raise WrapperError("invalid process identity")
    try:
        current = process_identity(recorded_pid)
    except WrapperError as exc:
        # A process disappearing between read and verification is stale and is
        # handled idempotently by stop_unit.  Direct callers still receive a
        # false result instead of an unsafe kill decision.
        if isinstance(exc.__cause__, ProcessLookupError):
            return False
        if "No such file" in str(exc) or "cannot read" in str(exc):
            return False
        raise
    return (
        current["pid"] == recorded_pid
        and current["start_ticks"] == recorded_ticks
        and current["exe"] == str(Path(recorded_exe).resolve(strict=False))
        and _process_argv_matches(
            tuple(current["argv"]), recorded_argv, executable=str(current["exe"]),
        )
    )


def _validate_unit_config(
    config: dict[str, object],
    *,
    spec: UnitSpec,
    expected_release_id: str | None = None,
) -> dict[str, object]:
    if set(config) - _CONFIG_FIELDS:
        raise WrapperError(f"{spec.name} config contains unsupported metadata")
    if config.get("schema") != UNIT_CONFIG_SCHEMA or config.get("unit") != spec.name:
        raise WrapperError(f"{spec.name} config identity mismatch")
    config_release = config.get("release_id")
    if not isinstance(config_release, str):
        raise WrapperError(f"{spec.name} config release id is invalid")
    _release_id(config_release)
    if expected_release_id is not None and config_release != _release_id(expected_release_id):
        raise WrapperError(f"{spec.name} config release id mismatch")
    state = config.get("state")
    if state not in {"stopped", "starting", "running", "failed"}:
        raise WrapperError(f"{spec.name} config state is invalid")
    pid = config.get("pid")
    ticks = config.get("start_ticks")
    exe = config.get("exe")
    argv = config.get("argv")
    digest = config.get("command_sha256")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise WrapperError(f"{spec.name} config PID is invalid")
    if ticks is not None and (isinstance(ticks, bool) or not isinstance(ticks, int) or ticks <= 0):
        raise WrapperError(f"{spec.name} config start ticks are invalid")
    if exe is not None and not isinstance(exe, str):
        raise WrapperError(f"{spec.name} config executable identity is invalid")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise WrapperError(f"{spec.name} config argv identity is invalid")
    if digest is not None and not isinstance(digest, str):
        raise WrapperError(f"{spec.name} config command digest is invalid")
    if state == "stopped" and any(value is not None for value in (pid, ticks, exe)):
        raise WrapperError(f"{spec.name} stopped config retains a process identity")
    if state != "stopped" and (pid is None or ticks is None or exe is None or not argv or digest is None):
        raise WrapperError(f"{spec.name} active config lacks a complete process identity")
    return config


def _read_unit_config_bytes(
    content: bytes,
    *,
    spec: UnitSpec,
    expected_release_id: str | None = None,
) -> dict[str, object]:
    if len(content) > 128 * 1024:
        raise WrapperError(f"{spec.name} config is too large")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WrapperError(f"{spec.name} config is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WrapperError(f"{spec.name} config must contain an object")
    return _validate_unit_config(
        value,
        spec=spec,
        expected_release_id=expected_release_id,
    )


def _read_unit_config(path: Path, *, spec: UnitSpec, expected_release_id: str | None = None) -> dict[str, object]:
    _assert_destination(path, label=f"{spec.name} config", mode=UNIT_CONFIG_MODE)
    config = _load_json_file(path, label=f"{spec.name} config", max_bytes=128 * 1024)
    return _validate_unit_config(
        config,
        spec=spec,
        expected_release_id=expected_release_id,
    )


@dataclass(frozen=True)
class _UnitConfigSnapshot:
    """The exact config leaf and parent identities behind a runtime read."""

    path: Path
    preimage: object
    parent_guards: object


def _read_unit_config_snapshot(
    path: Path,
    *,
    spec: UnitSpec,
    expected_release_id: str | None = None,
) -> tuple[dict[str, object], _UnitConfigSnapshot]:
    """Read runtime metadata with the compare-and-swap token for its update."""

    path = _path(path, label=f"{spec.name} config")
    api = _release_publication_api()
    try:
        guards = api._capture_publication_parent_guards(  # type: ignore[attr-defined]
            (path,),
            create_parents=False,
        )
        preimage = api._capture_anchored_files_safely(  # type: ignore[attr-defined]
            ((path, f"{spec.name} config"),),
            parent_guards=guards,
        )[path]
    except Exception as exc:
        raise WrapperError(f"could not capture {spec.name} config: {exc}") from exc
    captured = preimage.snapshot
    if not captured.exists:
        raise WrapperError(f"{spec.name} config is missing: {path}")
    if captured.mode != UNIT_CONFIG_MODE:
        raise WrapperError(
            f"{spec.name} config has mode {captured.mode:04o}; "
            f"expected {UNIT_CONFIG_MODE:04o}: {path}"
        )
    config = _read_unit_config_bytes(
        captured.content,
        spec=spec,
        expected_release_id=expected_release_id,
    )
    return config, _UnitConfigSnapshot(path, preimage, guards)


def _assert_unit_config_snapshot(
    snapshot: _UnitConfigSnapshot,
    *,
    spec: UnitSpec,
    expected_release_id: str | None,
) -> None:
    """Reject a concurrent config replacement before acting on a PID.

    The initial config read is the compare-and-swap token for a stop.  A
    process identity check can race a concurrent start that publishes a new
    record, so an unchanged PID check is insufficient on its own.  Re-read
    the leaf through its anchored parent chain immediately before clearing
    metadata or sending a signal and require the complete preimage to match.
    """

    try:
        _config, current = _read_unit_config_snapshot(
            snapshot.path,
            spec=spec,
            expected_release_id=expected_release_id,
        )
    except Exception as exc:
        raise WrapperError(f"{spec.name} config changed after preflight") from exc
    if current.preimage != snapshot.preimage:
        raise WrapperError(f"{spec.name} config changed after preflight")


def _validate_recorded_command(
    spec: UnitSpec,
    layout: WrapperLayout,
    config: Mapping[str, object],
) -> None:
    """Check that a live process identity still describes this unit policy."""

    expected = build_start_command(
        spec,
        source_root=layout.source_root,
        execution_root=layout.execution_root,
        execution_binding=_execution_binding_payload(layout),
        control_root=layout.control_root,
        runtime_root=layout.runtime_root,
        state_root=layout.state_root,
        managed_root=layout.managed_root,
        managed_bin=layout.managed_bin,
        unit_dir=layout.unit_dir,
    )
    expected_argv = _expected_proc_argv(expected)
    actual = config.get("argv")
    if (
        spec.kind == "recovery"
        and isinstance(actual, list)
        and len(actual) == len(expected_argv)
        and actual[5] != expected_argv[5]
        and tuple(actual[:5]) == expected_argv[:5]
        and tuple(actual[6:]) == expected_argv[6:]
    ):
        # Publication carries A's running watchdog identity into B's config.
        # Its import root may still name A, but every other command argument
        # remains governed by this unit's exact policy.  A config-supplied
        # path is not authority: accept only a freshly verified installed
        # release below this same manager's releases directory.
        recorded_root = _path(actual[5], label="recorded recovery execution root")
        releases_root = layout.managed_root / "releases"
        if recorded_root.parent.parent != releases_root or recorded_root.name != "site":
            raise WrapperError("recorded recovery execution root is not a managed release site")
        recorded_release = _release_id(recorded_root.parent.name)
        release_manager = _release_publication_api()
        try:
            previous_binding = release_manager._release_wrapper_execution_kwargs(
                release_manager.release_layout(root=layout.managed_root, bin_dir=layout.managed_bin),
                recorded_release,
            )
        except (OSError, ValueError, release_manager.ReleaseError) as exc:
            raise WrapperError(f"recorded recovery release could not be verified: {exc}") from exc
        if (
            previous_binding.get("execution_root") != recorded_root
            or not previous_binding.get("execution_binding")
        ):
            raise WrapperError("recorded recovery release lacks an immutable execution binding")
        expected_argv = (*expected_argv[:5], str(recorded_root), *expected_argv[6:])
    if not isinstance(actual, list) or tuple(str(item) for item in actual) != expected_argv:
        raise WrapperError(f"{spec.name} recorded argv does not match its command policy")
    digest = config.get("command_sha256")
    if digest != _sha256_bytes(_canonical_json(list(expected_argv))):
        raise WrapperError(f"{spec.name} recorded command digest does not match its policy")
    executable = config.get("exe")
    if not isinstance(executable, str) or executable != _expected_proc_executable(expected[0]):
        raise WrapperError(f"{spec.name} recorded executable does not match its command policy")


def _read_nginx_pid(layout: WrapperLayout) -> int:
    pid_path = _runtime_paths(layout)["nginx_pid"]
    _assert_destination(pid_path, label="nginx pid file")
    if not os.path.lexists(pid_path):
        raise WrapperError(f"nginx pid file is missing: {pid_path}")
    try:
        raw = pid_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise WrapperError(f"could not read nginx pid file: {pid_path}") from exc
    if not raw.isdecimal():
        raise WrapperError(f"nginx pid file is malformed: {pid_path}")
    pid = int(raw)
    if pid <= 0:
        raise WrapperError(f"nginx pid file has an invalid PID: {pid_path}")
    return pid


def _atomic_write_json(
    path: Path,
    value: Mapping[str, object],
    *,
    mode: int,
    snapshot: _UnitConfigSnapshot | None = None,
) -> None:
    # Delegate the actual fsync/rename mechanics to the release manager's
    # primitive through a one-file transaction.  This also preserves its
    # no-follow and rollback checks.
    if snapshot is not None and (
        not isinstance(snapshot, _UnitConfigSnapshot) or snapshot.path != path
    ):
        raise WrapperError("unit config update snapshot does not match its destination")
    content = _canonical_json(value)
    if snapshot is not None and _publish_activation_config_transition(snapshot, content, mode):
        return
    _publish_files_transactionally(
        ((path, content, mode, f"unit config {path.name}"),),
        parent_guards=snapshot.parent_guards if snapshot is not None else None,
        expected_preimages={path: snapshot.preimage} if snapshot is not None else None,
    )


_CONFIG_TRANSITION_SCHEMA = "termdeck.activation-config-transition.v1"
_CONFIG_CHECKPOINT_SCHEMA = "termdeck.activation-config-checkpoint.v1"
_CONFIG_TRANSITION_LIMIT = 64


def _config_transition_path(tx: Path, image: Mapping[str, object]) -> Path:
    return tx / ("config-transition-" + _sha256_bytes(_canonical_json(image)) + ".json")


def _config_checkpoint_path(tx: Path, root: Mapping[str, object]) -> Path:
    return tx / ("config-checkpoint-" + _sha256_bytes(_canonical_json(root)) + ".json")


def _config_transition_read(path: Path):
    from . import web_activation as api
    _path, capture = api._capture_anchored(path, label="config transition receipt")
    if not capture.snapshot.exists:
        return None, capture
    if capture.snapshot.mode != 0o600 or len(capture.snapshot.content) > 128 * 1024:
        raise WrapperError("config transition receipt is unsafe or too large")
    return api._strict_json_object(capture.snapshot.content, label="config transition receipt"), capture


def _config_transition_save(path: Path, value: Mapping[str, object], previous: object):
    from . import web_activation as api
    guards = _release_publication_api()._capture_publication_parent_guards((path,), create_parents=False)
    api._publish_anchored(path, _canonical_json(value), 0o600,
                          label="config transition receipt", parent_guards=guards, expected_preimage=previous)
    return _config_transition_read(path)[1]


def _config_transition_handoff(tx, path, handoff, parent_guards):
    from . import web_activation as api
    if not isinstance(handoff, dict) or set(handoff) != {
        "journal_path", "journal_identity", "state", "parents", "preimages", "postimages",
    }:
        raise WrapperError("config transition publication is invalid")
    before = api._release_publication_image_map(handoff, "preimages")
    if set(before) != {path} or before[path].get("exists") is not True:
        raise WrapperError("config transition predecessor is invalid")
    guards = api._release_publication_parent_guards_from_payload(handoff["parents"])
    if len(guards.guards) != 1 or guards.for_path(path) != parent_guards.for_path(path):
        raise WrapperError("config transition parent changed")
    journal = path.parent / (".termdeck-publication-config-" + _sha256_bytes(_canonical_json([str(tx), before[path]])) + ".json")
    if handoff.get("journal_path") != str(journal):
        raise WrapperError("config transition journal path changed")
    identity = api._file_identity_from_payload(handoff.get("journal_identity"))
    if identity is None:
        raise WrapperError("config transition journal identity is missing")
    return before[path], guards, journal, identity


@contextmanager
def _config_transition_lock(path: Path, guards: object):
    manager = _release_publication_api()
    parents = manager._open_publication_parents((path,), parent_guards=guards, create_parents=False)
    descriptor = -1
    try:
        parent = parents.for_path(path)
        name = "." + path.name + ".lifecycle.lock"
        descriptor = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent.descriptor)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or stat.S_IMODE(observed.st_mode) != 0o600 or observed.st_uid != os.geteuid():
            raise WrapperError("config lifecycle lock is unsafe")
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WrapperError("config lifecycle transition is busy")
                time.sleep(0.02)
        named = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino, named.st_nlink, named.st_mode) != (observed.st_dev, observed.st_ino, 1, observed.st_mode):
            raise WrapperError("config lifecycle lock changed")
        parents.assert_current(phase="before config lifecycle transition")
        yield
        parents.assert_current(phase="after config lifecycle transition")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        parents.close()


def reconcile_activation_config(tx: Path, path: Path, root: Mapping[str, object], parent_guards: object):
    with _config_transition_lock(path, parent_guards):
        return _reconcile_activation_config_locked(tx, path, root, parent_guards)


def _reconcile_activation_config_locked(
    tx: Path, path: Path, root: Mapping[str, object], parent_guards: object,
) -> tuple[object, int]:
    """Follow exact CAS receipts from the install image to the current config.

    Receipts are bounded to this transaction and six config destinations.
    A retained inner journal is settled before its output is admitted. No
    content-only comparison grants ownership of a replacement inode.
    """
    from . import web_activation as api
    manager = _release_publication_api()
    if path.name not in UNIT_CONFIG_NAMES or root.get("path") != str(path):
        raise WrapperError("config transition has an invalid destination")
    checkpoint_path = _config_checkpoint_path(tx, root)
    saved, checkpoint_capture = _config_transition_read(checkpoint_path)
    head = dict(root)
    pending_retirements = {}
    if saved is not None:
        if (set(saved) != {"schema", "transaction", "root", "head", "receipt", "retired"}
                or saved.get("schema") != _CONFIG_CHECKPOINT_SCHEMA
                or saved.get("transaction") != str(tx) or saved.get("root") != root):
            raise WrapperError("config lifecycle checkpoint root changed")
        handoff = saved.get("receipt")
        _before, _guards, _journal, _identity = _config_transition_handoff(tx, path, handoff, parent_guards)
        if handoff.get("state") not in {"published", "recovered"}:
            raise WrapperError("config lifecycle checkpoint has no settled publication")
        images = api._release_publication_image_map(handoff, "postimages")
        if (set(images) != {path} or images[path].get("exists") is not True
                or saved.get("head") != images[path]):
            raise WrapperError("config lifecycle checkpoint handoff changed")
        pending_retirements = api._release_publication_image_map(saved, "retired")
        if len(pending_retirements) > _CONFIG_TRANSITION_LIMIT:
            raise WrapperError("config lifecycle checkpoint retirement bound is invalid")
        for entry, expected in pending_retirements.items():
            name = entry.name.removeprefix("config-transition-").removesuffix(".json")
            if (entry.parent != tx or entry.name != "config-transition-" + name + ".json"
                    or not api._lower_hex(name, length=64) or expected.get("exists") is not True):
                raise WrapperError("config lifecycle checkpoint retirement destination is invalid")
        head = images[path]
    consumed = []
    last_handoff = None

    def finish(current, count):
        nonlocal checkpoint_capture
        # The exact current leaf has just been verified from the prior
        # checkpoint through every inner CAS. Publish the new authority before
        # retiring that prefix. The install root never changes. A crash before
        # this CAS keeps the old chain; a crash afterward starts from this
        # exact callback postimage, including its inode. The limit bounds only
        # unreconciled work, never the lifetime number of service restarts.
        def retire(images):
            for entry, expected in images.items():
                _entry, captured = api._capture_anchored(entry, label="retired config transition receipt")
                if not captured.snapshot.exists:
                    continue  # A previous cleanup may have completed before its process exited.
                if api._release_publication_snapshot_payload(entry, captured) != expected:
                    raise WrapperError("retired config transition receipt inode changed")
                guards = manager._capture_publication_parent_guards((entry,), create_parents=False)
                api._publish_anchored(entry, None, 0o600, label="retired config transition receipt",
                                      parent_guards=guards, expected_preimage=captured)

        retire(pending_retirements)
        if pending_retirements:
            completed = dict(saved)
            completed["retired"] = []
            checkpoint_capture = _config_transition_save(checkpoint_path, completed, checkpoint_capture)
        if last_handoff is not None and not os.path.lexists(last_handoff["journal_path"]):
            retirements = {entry: api._release_publication_snapshot_payload(entry, captured)
                           for entry, captured in consumed}
            value = {"schema": _CONFIG_CHECKPOINT_SCHEMA, "transaction": str(tx),
                     "root": dict(root), "head": api._release_publication_snapshot_payload(path, current),
                     "receipt": last_handoff, "retired": list(retirements.values())}
            checkpoint_capture = _config_transition_save(checkpoint_path, value, checkpoint_capture)
            retire(retirements)
            value["retired"] = []
            _config_transition_save(checkpoint_path, value, checkpoint_capture)
            return current, 0
        return current, count

    seen = set()
    for count in range(_CONFIG_TRANSITION_LIMIT + 1):
        entry = _config_transition_path(tx, head)
        if entry in seen:
            raise WrapperError("config transition chain contains a cycle")
        seen.add(entry)
        row, captured = _config_transition_read(entry)
        if row is None:
            _path, current = api._capture_anchored(path, label="activation config", parent_guards=parent_guards)
            if api._release_publication_snapshot_payload(path, current) != head:
                raise WrapperError("activation config changed without a lifecycle receipt")
            return finish(current, count)
        if count == _CONFIG_TRANSITION_LIMIT:
            raise WrapperError("config transition chain exceeds its bound")
        if set(row) != {"schema", "transaction", "root", "receipt"} or row.get("schema") != _CONFIG_TRANSITION_SCHEMA or row.get("transaction") != str(tx) or row.get("root") != head:
            raise WrapperError("config transition receipt identity changed")
        handoff = row.get("receipt")
        before, guards, journal, expected_identity = _config_transition_handoff(tx, path, handoff, parent_guards)
        if before != head:
            raise WrapperError("config transition predecessor changed")
        _journal, current_journal = api._capture_anchored(journal, label="config transition journal", parent_guards=guards)
        if current_journal.snapshot.exists:
            if expected_identity is None or current_journal.identity is None or not api._same_publication_journal_inode(current_journal.identity, expected_identity):
                raise WrapperError("config transition journal inode changed")

            def recovered(publication):
                nonlocal captured, handoff
                value = api._release_publication_receipt_payload(publication)
                if (value["preimages"] != handoff["preimages"] or value["parents"] != handoff["parents"]
                        or value["journal_path"] != handoff["journal_path"]
                        or not api._same_publication_journal_inode(publication.journal_identity, expected_identity)):
                    raise WrapperError("config transition recovery handoff changed")
                handoff = value
                row["receipt"] = value
                captured = _config_transition_save(entry, row, captured)

            outcome = manager.recover_publication_transaction(journal, after_recovery=recovered)
            if outcome.get("ok") is not True:
                raise WrapperError("config transition journal recovery failed")
        if handoff.get("state") not in {"published", "recovered"}:
            raise WrapperError("config transition lacks a durable postimage")
        images = api._release_publication_image_map(handoff, "postimages")
        if set(images) != {path} or images[path].get("exists") is not True:
            raise WrapperError("config transition postimage is invalid")
        successor = images[path]
        consumed.append((entry, captured))
        last_handoff = handoff
        if successor == head:
            # An intent-only crash can recover without changing the leaf.
            _path, current = api._capture_anchored(path, label="activation config", parent_guards=parent_guards)
            if api._release_publication_snapshot_payload(path, current) != head:
                raise WrapperError("activation config changed after lifecycle recovery")
            return finish(current, count + 1)
        head = successor
    raise WrapperError("config transition chain exceeds its bound")


def _activation_config_context(path: Path):
    from . import web_activation as api
    evidence = path.parent / "recovery" / "activation-v2"
    latest = evidence / "LATEST"
    if not os.path.lexists(latest):
        return None
    _latest, capture = api._read_regular(latest, label="activation transaction pointer")
    pointer_identity = capture.identity
    pointer = api._strict_json_object(capture.snapshot.content, label="activation transaction pointer")
    name = pointer.get("transaction")
    if not isinstance(name, str) or not name.startswith("tx-") or Path(name).name != name:
        raise WrapperError("activation config transaction pointer is invalid")
    tx = evidence / name
    _receipt, capture = api._read_regular(tx / "receipt.json", label="activation receipt")
    receipt = api._strict_json_object(capture.snapshot.content, label="activation receipt")
    if receipt.get("schema") != api.SCHEMA or receipt.get("tx_id") != name or Path(str(receipt.get("database"))).parent != path.parent:
        raise WrapperError("activation config transaction identity is invalid")
    if not receipt.get("mutation_started") or receipt.get("status") == "rolled-back" or receipt.get("rollback_service_restart") is not None:
        return None
    if path.name not in UNIT_CONFIG_NAMES:
        raise WrapperError("activation config destination is invalid")
    record = receipt.get("preimages", {}).get("wrapper:" + str(path))
    if not isinstance(record, dict):
        raise WrapperError("activation config lacks its outer preimage")
    parsed = api._release_publication_record_state(receipt, key="package_publication")
    if parsed is None or parsed[0].get("state") != "published":
        raise WrapperError("activation config lacks its install receipt")
    root = api._release_callback_postimage(api._release_publication_recovered_handoff(parsed[0]), path)
    authority = (receipt.get("status"), receipt.get("mutation_started"),
                 receipt.get("rollback_service_restart"), pointer_identity)
    return tx, root, api._record_parent_guards(record, path), authority


def _publish_activation_config_transition(snapshot: _UnitConfigSnapshot, content: bytes, mode: int) -> bool:
    """Persist inner publication handoffs without racing the outer heartbeat."""
    try:
        context = _activation_config_context(snapshot.path)
        if context is None:
            return False
        tx, root, guards, _authority = context
        with _config_transition_lock(snapshot.path, guards):
            if _activation_config_context(snapshot.path) != context:
                raise WrapperError("activation config context changed while acquiring lifecycle lock")
            return _publish_activation_config_locked(snapshot, content, mode, context)
    except Exception as exc:
        if isinstance(exc, WrapperError):
            raise
        raise WrapperError(f"activation config transition failed: {exc}") from exc


def _publish_activation_config_locked(snapshot, content, mode, context):
    from . import web_activation as api
    manager = _release_publication_api()
    tx, root, guards, _authority = context
    current, count = _reconcile_activation_config_locked(tx, snapshot.path, root, guards)
    if current != snapshot.preimage or count >= _CONFIG_TRANSITION_LIMIT:
        raise WrapperError("activation config changed after lifecycle preflight")
    spec = UNIT_SPEC_BY_NAME[snapshot.path.name[:-5]]
    previous = _read_unit_config_bytes(current.snapshot.content, spec=spec)
    _read_unit_config_bytes(content, spec=spec, expected_release_id=previous["release_id"])
    head = api._release_publication_snapshot_payload(snapshot.path, current)
    entry = _config_transition_path(tx, head)
    old, captured = _config_transition_read(entry)
    if old is not None and (old.get("receipt", {}).get("state") != "recovered" or api._release_publication_image_map(old["receipt"], "postimages") != {snapshot.path: head}):
        raise WrapperError("config transition predecessor already has a successor")
    row = {"schema": _CONFIG_TRANSITION_SCHEMA, "transaction": str(tx), "root": head, "receipt": {}}
    journal = snapshot.path.parent / (".termdeck-publication-config-" + _sha256_bytes(_canonical_json([str(tx), head])) + ".json")

    def checkpoint(publication):
        nonlocal captured
        if publication.state == "intent":
            assert_context()
        row["receipt"] = api._release_publication_receipt_payload(publication)
        captured = _config_transition_save(entry, row, captured)

    def assert_context():
        if _activation_config_context(snapshot.path) != context:
            raise WrapperError("activation config context changed before lifecycle publication")

    manager._publish_files_transactionally(
        ((snapshot.path, content, mode, f"unit config {snapshot.path.name}"),),
        parent_guards=snapshot.parent_guards, expected_preimages={snapshot.path: snapshot.preimage},
        journal_path=journal, before_publish=checkpoint, after_publish=checkpoint, after_recovery=checkpoint,
        verify=assert_context,
    )
    _reconcile_activation_config_locked(tx, snapshot.path, root, guards)
    return True


def _carry_runtime_identity(
    spec: UnitSpec,
    path: Path,
    rendered: bytes,
    *,
    existing_content: bytes | None = None,
) -> bytes:
    """Carry an existing live identity across a wrapper release publication.

    Activation publishes the new stop wrappers before asking systemd to stop
    the old web processes.  Clearing the old PID metadata at that boundary
    would make the newly published stop wrapper unable to perform that
    bounded, identity-checked stop.  Only the runtime identity fields are
    carried; unit and release binding always come from the new render.
    """

    if existing_content is None:
        if not os.path.lexists(path):
            return rendered
        old = _read_unit_config(path, spec=spec)
    else:
        old = _read_unit_config_bytes(existing_content, spec=spec)
    if old.get("state") == "stopped":
        return rendered
    identity_keys = ("pid", "start_ticks", "exe", "argv", "command_sha256")
    if any(old.get(key) in (None, [], "") for key in identity_keys):
        return rendered
    try:
        value = json.loads(rendered.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - render is internal
        raise WrapperError(f"rendered {spec.name} config is invalid") from exc
    if not isinstance(value, dict):  # pragma: no cover - render is internal
        raise WrapperError(f"rendered {spec.name} config is invalid")
    for key in identity_keys:
        value[key] = old[key]
    value["state"] = old.get("state", "starting")
    return _canonical_json(value)


def _record_start_identity(
    spec: UnitSpec,
    layout: WrapperLayout,
    release_id: str,
    command: Sequence[str],
) -> None:
    config_path = layout.config_path(spec)
    config, config_snapshot = _read_unit_config_snapshot(
        config_path,
        spec=spec,
        expected_release_id=release_id,
    )
    if not command:
        raise WrapperError("cannot record an empty service command")
    identity = {
        "pid": os.getpid(),
        "start_ticks": proc_start_ticks(os.getpid()),
        "exe": _expected_proc_executable(command[0]),
        "argv": list(_expected_proc_argv(command)),
        "command_sha256": _sha256_bytes(
            _canonical_json(list(_expected_proc_argv(command)))
        ),
    }
    config.update(identity)
    config["state"] = "starting"
    _atomic_write_json(
        config_path,
        config,
        mode=UNIT_CONFIG_MODE,
        snapshot=config_snapshot,
    )


def _checkpoint_group_is_running(pid: int) -> bool:
    """Inspect the private checkpoint group without reaping its leader.

    A zombie leader reserves its PID until cleanup has sent its last signal.
    Zombies do not count as surviving work, including orphaned grandchildren
    that the system's reaper may not have collected yet.
    """

    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == pid:
            if int(fields[3]) != pid:
                raise WrapperError("checkpoint process group is not its private session")
            if fields[0] not in {"Z", "X"}:
                return True
    return False


def _wait_checkpoint_group(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _checkpoint_group_is_running(pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _pidfd_is_exited(descriptor: int) -> bool:
    """Return whether the identity-bound leader has become waitable."""

    poller = select.poll()
    try:
        poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
        return bool(poller.poll(0))
    except (OSError, ValueError) as exc:
        raise WrapperError("could not inspect the checkpoint process identity") from exc


def _checkpoint_leader_was_reaped(descriptor: int) -> bool:
    """Distinguish a waitable zombie from a reaped identity-bound leader."""

    poller = select.poll()
    try:
        poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
        events = poller.poll(0)
    except (OSError, ValueError) as exc:
        raise WrapperError("could not inspect the checkpoint leader identity") from exc
    for _descriptor, event_mask in events:
        if event_mask & select.POLLNVAL:
            raise WrapperError("could not inspect the checkpoint leader identity")
        # A pidfd is readable once its process becomes a waitable zombie.  It
        # gains POLLHUP only after that zombie is reaped, which is the point
        # where the numeric process-group ID is no longer safe to signal.
        if event_mask & select.POLLHUP:
            return True
    return False


def _cleanup_checkpoint_process(process: subprocess.Popen[bytes]) -> bool:
    """Bound cleanup to this Popen's new session, then reap its leader."""

    pid = getattr(process, "pid", None)
    private_group = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 1
        and pid != os.getpgrp()
    )
    # A checkpoint leader's PID is also its process-group ID.  Keep an
    # identity-bound descriptor while deciding whether a group signal is safe;
    # if an external reaper consumed the WNOWAIT leader, the numeric group ID
    # may already belong to an unrelated process and must never be signaled.
    identity_pidfd = getattr(process, "_termdeck_checkpoint_pidfd", None)
    identity_bound = isinstance(identity_pidfd, int) and identity_pidfd >= 0
    if private_group and identity_bound and _pidfd_is_exited(identity_pidfd):
        if _checkpoint_leader_was_reaped(identity_pidfd):
            return False
    if private_group and hasattr(process, "_termdeck_checkpoint_pidfd") and not identity_bound:
        return False
    # Popen.wait() can reap the child while handling KeyboardInterrupt.  Once
    # reaped, its numeric PID is no longer reserved and cannot safely address
    # a process group.  Report surviving work without signaling that number.
    if getattr(process, "returncode", None) is not None:
        try:
            return bool(private_group and not _checkpoint_group_is_running(pid))
        except (OSError, WrapperError, ValueError, IndexError):
            return False

    interrupted: BaseException | None = None
    cleaned = False
    for sig, grace in (
        (signal.SIGTERM, CHECKPOINT_TERMINATE_GRACE),
        (signal.SIGKILL, CHECKPOINT_KILL_GRACE),
    ):
        group_signalled = False
        group_gone = False
        if private_group and identity_bound and _pidfd_is_exited(identity_pidfd):
            if _checkpoint_leader_was_reaped(identity_pidfd):
                break
        if private_group:
            try:
                _signal_with_eintr_retry(os.killpg, pid, sig)
                group_signalled = True
            except ProcessLookupError:
                group_gone = True
            except (OSError, WrapperError):
                pass
            except BaseException as exc:
                interrupted = interrupted or exc
                continue
        if not group_signalled and not group_gone:
            try:
                (process.terminate if sig == signal.SIGTERM else process.kill)()
            except (AttributeError, OSError):
                pass
            except BaseException as exc:
                interrupted = interrupted or exc
                continue
        try:
            if group_signalled and not _wait_checkpoint_group(pid, timeout=grace):
                if identity_bound and _checkpoint_leader_was_reaped(identity_pidfd):
                    # Another waiter reaped the leader before its group
                    # drained.  Do not escalate through a reusable group ID.
                    break
                continue
            # Do not reap a leader while its group is still running: its exit
            # alone says nothing about children that ignored SIGTERM.
            process.wait(timeout=grace)
            cleaned = group_signalled or group_gone or not private_group
            break
        except (subprocess.TimeoutExpired, OSError, WrapperError, ValueError, IndexError):
            pass
        except BaseException as exc:
            interrupted = interrupted or exc
        if getattr(process, "returncode", None) is not None or group_gone:
            break
    if not cleaned and getattr(process, "returncode", None) is None:
        # Reap the direct child even when surviving or uninspectable group
        # members prevent us from claiming that all cleanup has completed.
        try:
            process.wait(timeout=CHECKPOINT_KILL_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            pass
        except BaseException as exc:
            interrupted = interrupted or exc
    if interrupted is not None:
        raise interrupted
    return cleaned


def _close_checkpoint_pidfd(process: object) -> None:
    descriptor = getattr(process, "_termdeck_checkpoint_pidfd", None)
    if isinstance(descriptor, int) and descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            setattr(process, "_termdeck_checkpoint_pidfd", -1)
        except (AttributeError, TypeError):
            pass


def _wait_checkpoint_exit(process: subprocess.Popen[bytes], *, timeout: float) -> None:
    """Wait for exit while retaining the child PID for cancellation cleanup.

    Popen.wait() may reap a just-exited child while handling KeyboardInterrupt.
    WNOWAIT leaves the leader waitable, so cleanup can still safely address its
    private group if an interrupt arrives as it exits.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            status = os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except InterruptedError:
            status = None
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise
            status = None
        if status is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(min(0.05, remaining))


def _run_checkpoint(layout: WrapperLayout) -> int:
    command = build_stop_command(
        "termdeck-checkpoint.service",
        source_root=layout.source_root,
        execution_root=layout.execution_root,
        execution_binding=_execution_binding_payload(layout),
        control_root=layout.control_root,
        runtime_root=layout.runtime_root,
        state_root=layout.state_root,
        managed_root=layout.managed_root,
        managed_bin=layout.managed_bin,
        unit_dir=layout.unit_dir,
    )
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        print(f"termdeck checkpoint failed to start: {exc}", file=sys.stderr)
        return 127
    # Real Popen objects expose ``args``; the small fake process objects used
    # by offline wrapper tests do not.  Bind the real leader before any group
    # inspection so WNOWAIT cannot be followed by a numeric-PID reuse.
    if hasattr(process, "args"):
        try:
            ticks = proc_start_ticks(process.pid)
            setattr(
                process,
                "_termdeck_checkpoint_pidfd",
                _open_identity_pidfd(process.pid, ticks),
            )
        except WrapperError:
            setattr(process, "_termdeck_checkpoint_pidfd", -1)
    try:
        _wait_checkpoint_exit(process, timeout=CHECKPOINT_TIMEOUT)
        # An exited leader can still leave children in its group.  Finish
        # that work before reaping; an interrupt during the final wait then
        # cannot strand descendants or authorize a reused group number.
        checkpoint_pidfd = getattr(process, "_termdeck_checkpoint_pidfd", None)
        if hasattr(process, "_termdeck_checkpoint_pidfd") and not (
            isinstance(checkpoint_pidfd, int) and checkpoint_pidfd >= 0
        ):
            print("termdeck checkpoint process identity could not be bound", file=sys.stderr)
            return 127
        if (
            isinstance(checkpoint_pidfd, int)
            and checkpoint_pidfd >= 0
            and _pidfd_is_exited(checkpoint_pidfd)
        ) and _checkpoint_leader_was_reaped(checkpoint_pidfd):
            # An external reaper released the leader.  Its old numeric group
            # can now belong to an unrelated process, so do not signal it.
            returncode = process.wait(timeout=CHECKPOINT_KILL_GRACE)
        elif _checkpoint_group_is_running(process.pid):
            if not _cleanup_checkpoint_process(process):
                print("termdeck checkpoint process cleanup did not finish", file=sys.stderr)
                return 127
            returncode = process.wait(timeout=CHECKPOINT_KILL_GRACE)
        else:
            returncode = process.wait(timeout=CHECKPOINT_KILL_GRACE)
    except subprocess.TimeoutExpired:
        cleaned = _cleanup_checkpoint_process(process)
        detail = "" if cleaned else " (process cleanup did not finish)"
        print(
            f"termdeck checkpoint timed out after {CHECKPOINT_TIMEOUT:.0f} seconds{detail}",
            file=sys.stderr,
        )
        return 124
    except ChildProcessError as exc:
        # An external reaper (or SIGCHLD disposition) has released the PID.
        # Neither the old number nor its group can safely be signaled now.
        print(f"termdeck checkpoint is no longer a waitable child: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        _cleanup_checkpoint_process(process)
        print(f"termdeck checkpoint wait failed: {exc}", file=sys.stderr)
        return 127
    except BaseException:
        try:
            if not _cleanup_checkpoint_process(process):
                print("termdeck checkpoint process cleanup did not finish", file=sys.stderr)
        except BaseException as exc:
            print(f"termdeck checkpoint cleanup interrupted: {exc}", file=sys.stderr)
        raise
    finally:
        _close_checkpoint_pidfd(process)
    if returncode:
        print(f"termdeck checkpoint failed with exit code {returncode}", file=sys.stderr)
    return int(returncode)


def _signal_with_eintr_retry(
    operation: Callable[[int, int], object],
    pid: int,
    sig: int,
) -> None:
    """Issue one process signal without treating a transient EINTR as state."""

    for attempt in range(_SIGNAL_EINTR_RETRIES):
        try:
            operation(pid, sig)
            return
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINTR:
                raise
        if attempt + 1 >= _SIGNAL_EINTR_RETRIES:
            break
    raise WrapperError(
        f"process signal was interrupted {_SIGNAL_EINTR_RETRIES} times"
    )


def _process_exists(pid: int) -> bool:
    try:
        _signal_with_eintr_retry(os.kill, pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, *, timeout: float) -> bool:
    bounded = max(0.0, min(float(timeout), MAX_STOP_TIMEOUT))
    deadline = time.monotonic() + bounded
    while _process_exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


def _open_identity_pidfd(pid: int, start_ticks: int) -> int | None:
    """Open a descriptor for the verified process generation.

    A numeric PID is only a lookup key.  Between the /proc identity check and
    a later signal it may have exited and been reused.  ``pidfd_open`` binds
    the handle to the process object; the second start-ticks check closes the
    remaining gap where a new process wins the PID before the descriptor is
    returned.  A vanished process is an idempotent stop result, while any
    replacement is a hard refusal.
    """

    opener = getattr(os, "pidfd_open", None)
    if not callable(opener):
        raise WrapperError("stopping a recorded process requires pidfd_open")
    try:
        descriptor = int(opener(pid, 0))
    except ProcessLookupError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ESRCH, errno.ENOENT}:
            return None
        raise WrapperError(f"could not open a process identity descriptor for {pid}: {exc}") from exc
    try:
        try:
            observed_ticks = proc_start_ticks(pid)
        except WrapperError as exc:
            if isinstance(exc.__cause__, (FileNotFoundError, ProcessLookupError)):
                os.close(descriptor)
                return None
            if "No such file" in str(exc) or "cannot read" in str(exc):
                os.close(descriptor)
                return None
            raise
        if observed_ticks != start_ticks:
            raise WrapperError(f"process {pid} was replaced before its identity descriptor was bound")
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _pidfd_send_signal(descriptor: int, sig: int) -> None:
    """Send a signal through a process descriptor, retrying EINTR only."""

    sender = getattr(signal, "pidfd_send_signal", None)
    if not callable(sender):
        raise WrapperError("stopping a recorded process requires pidfd_send_signal")
    for attempt in range(_SIGNAL_EINTR_RETRIES):
        try:
            sender(descriptor, sig, None, 0)
            return
        except InterruptedError:
            pass
        except OSError as exc:
            if exc.errno in {errno.ESRCH, errno.ENOENT}:
                raise ProcessLookupError(exc.errno, os.strerror(exc.errno)) from exc
            if exc.errno != errno.EINTR:
                raise
        if attempt + 1 >= _SIGNAL_EINTR_RETRIES:
            break
    raise WrapperError(f"process signal was interrupted {_SIGNAL_EINTR_RETRIES} times")


def _wait_for_pidfd_exit(descriptor: int, *, timeout: float) -> bool:
    """Wait for this exact process object to exit without a PID lookup."""

    bounded = max(0.0, min(float(timeout), MAX_STOP_TIMEOUT))
    poller = select.poll()
    try:
        poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    except (OSError, ValueError) as exc:
        raise WrapperError("could not monitor the process identity descriptor") from exc
    deadline = time.monotonic() + bounded
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            events = poller.poll(int(remaining * 1000))
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise WrapperError("could not wait for the recorded process") from exc
        if events:
            return True
        if time.monotonic() >= deadline:
            return False


def stop_unit(
    unit: str | UnitSpec,
    *,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    release_id: str | None = None,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    root: PathLike | None = None,
) -> int:
    """Stop one recorded process after exact PID identity verification."""

    spec = _spec_for(unit)
    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        manifest_path=manifest_path,
        root=root,
    )
    for directory, label in (
        (layout.source_root, "source root"),
        (layout.execution_root, "wrapper execution root"),
        (layout.runtime_root, "runtime root"),
        (layout.state_root, "state root"),
        (layout.managed_root, "managed root"),
        (layout.managed_bin, "managed bin"),
        (layout.unit_dir, "unit directory"),
    ):
        _assert_directory(directory, label=label, must_exist=True)
    _assert_execution_binding(layout)
    _assert_control_binding(layout)
    if spec.kind == "checkpoint":
        return _run_checkpoint(layout)
    config_path = layout.config_path(spec)
    if not os.path.lexists(config_path):
        return 0
    expected_release = release_id
    if os.path.lexists(layout.manifest_path):
        manifest = _validate_manifest(layout, expected_release_id=release_id)
        _validate_manifest_entries(layout, manifest)
        expected_release = str(manifest["release_id"])
    else:
        raise WrapperError("wrapper manifest is missing")
    config, config_snapshot = _read_unit_config_snapshot(
        config_path, spec=spec, expected_release_id=expected_release
    )
    if config.get("state") == "stopped":
        # Older rendered configs can carry a stopped process's argv/digest.
        # They are not a live identity, and leaving them behind makes later
        # policy/readback treat stale command metadata as meaningful.
        if config.get("argv") != [] or config.get("command_sha256") is not None:
            config.update(
                {
                    "pid": None,
                    "start_ticks": None,
                    "exe": None,
                    "argv": [],
                    "command_sha256": None,
                    "state": "stopped",
                }
            )
            _atomic_write_json(config_path, config, mode=UNIT_CONFIG_MODE, snapshot=config_snapshot)
        return 0
    pid_value = config.get("pid")
    ticks_value = config.get("start_ticks")
    exe_value = config.get("exe")
    argv_value = config.get("argv")
    if pid_value is None or ticks_value is None or exe_value is None or not argv_value:
        return 0
    try:
        pid = int(pid_value)
        ticks = int(ticks_value)
    except (TypeError, ValueError) as exc:
        raise WrapperError(f"{spec.name} config has an invalid PID identity") from exc
    if pid <= 0 or ticks <= 0 or not isinstance(argv_value, list):
        raise WrapperError(f"{spec.name} config has an invalid PID identity")
    identity = {"pid": pid, "start_ticks": ticks, "exe": str(exe_value), "argv": argv_value}
    try:
        matches = verify_process_identity(identity)
    except WrapperError as exc:
        if "No such file" in str(exc) or "cannot read" in str(exc):
            matches = False
        else:
            raise
    if not matches:
        _assert_unit_config_snapshot(
            config_snapshot,
            spec=spec,
            expected_release_id=expected_release,
        )
        try:
            same_process = proc_start_ticks(pid) == ticks
        except WrapperError as exc:
            if not isinstance(exc.__cause__, (FileNotFoundError, ProcessLookupError)):
                raise
            same_process = False
        if same_process:
            # A start records the post-exec identity before exec.  Until that
            # handoff completes, an argv/executable mismatch cannot establish
            # that the same live process generation is stale.
            raise WrapperError(f"{spec.name} live process identity changed; refusing to clear it")
        # PID absent or reused by another process: leave it alone and clear the
        # stale metadata so the next stop remains idempotent.
        config.update({"pid": None, "start_ticks": None, "exe": None, "argv": [], "command_sha256": None, "state": "stopped"})
        _atomic_write_json(config_path, config, mode=UNIT_CONFIG_MODE, snapshot=config_snapshot)
        return 0
    _validate_recorded_command(spec, layout, config)
    if spec.kind == "lan":
        nginx_pid = _read_nginx_pid(layout)
        if nginx_pid != pid:
            raise WrapperError(
                f"nginx pid file points at {nginx_pid}, expected recorded PID {pid}"
            )
    elif spec.kind == "cloudflared":
        _private_config(_runtime_paths(layout)["cloudflared_config"], label="cloudflared configuration")
    pidfd = _open_identity_pidfd(pid, ticks)
    if pidfd is None:
        matches = False
    else:
        try:
            try:
                _pidfd_send_signal(pidfd, spec.stop_signal)
            except ProcessLookupError:
                matches = False
            except OSError as exc:
                raise WrapperError(
                    f"could not signal {spec.name} process {pid}: {exc}"
                ) from exc
            if matches and not _wait_for_pidfd_exit(pidfd, timeout=stop_timeout):
                raise WrapperError(f"timed out waiting for {spec.name} process {pid}")
        finally:
            try:
                os.close(pidfd)
            except OSError:
                pass
    _assert_unit_config_snapshot(
        config_snapshot,
        spec=spec,
        expected_release_id=expected_release,
    )
    config.update({"pid": None, "start_ticks": None, "exe": None, "argv": [], "command_sha256": None, "state": "stopped"})
    _atomic_write_json(config_path, config, mode=UNIT_CONFIG_MODE, snapshot=config_snapshot)
    return 0


def run_wrapper(
    unit: str | UnitSpec,
    action: str,
    *,
    release_id: str | None = None,
    source_root: PathLike | None = None,
    execution_root: PathLike | None = None,
    execution_binding: Mapping[str, object] | None = None,
    control_root: PathLike | None = None,
    runtime_root: PathLike | None = None,
    state_root: PathLike | None = None,
    managed_root: PathLike | None = None,
    managed_bin: PathLike | None = None,
    unit_dir: PathLike | None = None,
    manifest_path: PathLike | None = None,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    root: PathLike | None = None,
) -> int:
    """Execute one generated wrapper's start or stop action."""

    spec = _spec_for(unit)
    if action not in {"start", "stop"}:
        raise WrapperError(f"unknown wrapper action: {action!r}")
    layout = resolve_layout(
        source_root=source_root,
        execution_root=execution_root,
        execution_binding=execution_binding,
        control_root=control_root,
        runtime_root=runtime_root,
        state_root=state_root,
        managed_root=managed_root,
        managed_bin=managed_bin,
        unit_dir=unit_dir,
        manifest_path=manifest_path,
        root=root,
    )
    if action == "stop":
        return stop_unit(
            spec,
            source_root=layout.source_root,
            execution_root=layout.execution_root,
            execution_binding=_execution_binding_payload(layout),
            control_root=layout.control_root,
            runtime_root=layout.runtime_root,
            state_root=layout.state_root,
            managed_root=layout.managed_root,
            managed_bin=layout.managed_bin,
            unit_dir=layout.unit_dir,
            manifest_path=layout.manifest_path,
            release_id=release_id,
            stop_timeout=stop_timeout,
        )

    for directory, label in (
        (layout.source_root, "source root"),
        (layout.execution_root, "wrapper execution root"),
        (layout.runtime_root, "runtime root"),
        (layout.state_root, "state root"),
        (layout.managed_root, "managed root"),
        (layout.managed_bin, "managed bin"),
        (layout.unit_dir, "unit directory"),
    ):
        _assert_directory(directory, label=label, must_exist=True)
    _assert_execution_binding(layout)
    _assert_control_binding(layout)
    # The path is immutable wrapper metadata, not a credential.  It reaches
    # the LAN preparer and recovery activation through their inherited env so
    # they can contact the root attestor without ever reading a raw HMAC key.
    os.environ[CONTROL_ROOT_ENV] = str(layout.control_root)
    if spec.kind == "recovery":
        # The watchdog passes explicit database/evidence paths.  Nested
        # release rollback must inherit the same configured state tree.
        os.environ["TERMDECK_STATE_ROOT"] = str(layout.state_root)
    if spec.kind == "checkpoint":
        # The start side has no command, writes no config, and intentionally
        # does not require a manifest so a stopped manager can always be safe.
        return 0
    expected_release = release_id
    if expected_release is None:
        if not os.path.lexists(layout.manifest_path):
            raise WrapperError("wrapper manifest is missing")
        manifest = _validate_manifest(layout)
        _validate_manifest_entries(layout, manifest)
        expected_release = str(manifest["release_id"])
    else:
        _release_id(expected_release)
        manifest = _validate_manifest(layout, expected_release_id=expected_release)
        _validate_manifest_entries(layout, manifest)
    command = _preflight_unit_start(spec, layout)
    _record_start_identity(spec, layout, expected_release, command)
    try:
        os.execv(command[0], list(command))
    except OSError as exc:
        # The process remains this wrapper only when exec fails.  Preserve a
        # stopped/failed marker for a subsequent bounded stop call.
        config_path = layout.config_path(spec)
        config, config_snapshot = _read_unit_config_snapshot(
            config_path, spec=spec, expected_release_id=expected_release
        )
        if (
            config.get("pid") == os.getpid()
            and config.get("start_ticks") == proc_start_ticks(os.getpid())
            and config.get("state") == "starting"
        ):
            config["state"] = "failed"
            _atomic_write_json(config_path, config, mode=UNIT_CONFIG_MODE, snapshot=config_snapshot)
        raise WrapperError(f"could not exec {spec.name}: {exc}") from exc
    raise AssertionError("execv returned")


def main(*, unit: str, action: str, **kwargs: Any) -> int:
    """Console entry point used by generated files."""

    try:
        return run_wrapper(unit, action, **kwargs)
    except WrapperError as exc:
        print(f"termdeck {unit} {action} wrapper failed: {exc}", file=sys.stderr)
        return 3


# Names used by a few callers that treat the module as a small service API.
render = render_wrappers
preflight = preflight_wrappers
readback = readback_wrappers
restore = restore_wrappers
render_wrapper_set = render_wrappers
publish_wrapper_set = publish_wrappers
preflight_wrapper_set = preflight_wrappers
readback_wrapper_set = readback_wrappers
restore_wrapper_set = restore_wrappers
unit_start_command = build_start_command
unit_stop_command = build_stop_command
read_proc_start_ticks = proc_start_ticks
get_process_identity = process_identity
validate_process_identity = verify_process_identity


__all__ = [
    "CANONICAL_MANAGED_BIN",
    "CANONICAL_MANAGED_ROOT",
    "CANONICAL_CONTROL_ROOT",
    "CANONICAL_RUNTIME_ROOT",
    "CANONICAL_SOURCE_ROOT",
    "CANONICAL_STATE_ROOT",
    "CANONICAL_STABLE_LAUNCHER",
    "DEFAULT_UNIT_DIR",
    "CONTROL_ATTEST_OPERATION",
    "CONTROL_ATTEST_EXCLUDED_PRINCIPAL",
    "CONTROL_ATTEST_OWNER",
    "CONTROL_ATTEST_PRODUCER",
    "CONTROL_ATTEST_GROUP",
    "CONTROL_ATTEST_REQUEST_SCHEMA",
    "CONTROL_ATTEST_RESPONSE_SCHEMA",
    "CONTROL_ATTEST_SOCKET_MODE",
    "CONTROL_ATTEST_SOCKET_NAME",
    "CONTROL_ROOT_MODE",
    "CONTROL_ROOT_ENV",
    "FileSnapshot",
    "RenderedWrappers",
    "REQUIRED_UNITS",
    "TERMDECK_BIN_DIR",
    "TERMDECK_MANAGED_ROOT",
    "TERMDECK_RUNTIME_ROOT",
    "TERMDECK_SOURCE_ROOT",
    "TERMDECK_STATE_ROOT",
    "TERMDECK_STABLE_LAUNCHER",
    "UNIT_CONFIG_MODE",
    "UNIT_CONFIG_NAMES",
    "UNIT_CONFIG_SCHEMA",
    "UNIT_NAMES",
    "UNIT_SPECS",
    "WRAPPER_MANIFEST_NAME",
    "WRAPPER_MANIFEST_SCHEMA",
    "WRAPPER_MODE",
    "WRAPPER_NAMES",
    "WRAPPER_RELATIVE_NAMES",
    "WrapperError",
    "WrapperLayout",
    "WrapperPublicationPlan",
    "build_start_command",
    "build_stop_command",
    "control_attestation_binding",
    "capture_wrapper_snapshot",
    "main",
    "preflight",
    "preflight_wrappers",
    "prepare_wrapper_publication",
    "proc_argv",
    "proc_executable",
    "proc_start_ticks",
    "read_proc_start_ticks",
    "process_identity",
    "get_process_identity",
    "publish",
    "publish_wrappers",
    "readback",
    "readback_wrappers",
    "read_process_identity",
    "render",
    "render_unit_configs",
    "render_wrapper_files",
    "render_wrappers",
    "resolve_layout",
    "restore",
    "restore_wrappers",
    "render_wrapper_set",
    "publish_wrapper_set",
    "preflight_wrapper_set",
    "readback_wrapper_set",
    "restore_wrapper_set",
    "run_wrapper",
    "start_command",
    "stop_command",
    "stop_unit",
    "unit_start_command",
    "unit_stop_command",
    "validate_process_identity",
    "verify_process_identity",
]
